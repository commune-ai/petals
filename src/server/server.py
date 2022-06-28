from __future__ import annotations

import multiprocessing as mp
import threading
from typing import Dict, Optional, Sequence, Union

import torch
from hivemind import DHT, MAX_DHT_TIME_DISCREPANCY_SECONDS, BatchTensorDescriptor, get_dht_time
from hivemind.moe.server.dht_handler import DHTHandlerThread
from hivemind.moe.server.layers import add_custom_models_from_file
from hivemind.moe.server.runtime import Runtime
from hivemind.proto.runtime_pb2 import CompressionType
from hivemind.utils.logging import get_logger, use_hivemind_log_handler

from src import declare_active_modules
from src.bloom.from_pretrained import DTYPE_MAP, DistributedBloomConfig, load_pretrained_block
from src.server.backend import TransformerBackend
from src.server.cache import MemoryCache
from src.server.handler import TransformerConnectionHandler

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__file__)


class Server(threading.Thread):
    """Serves one or more bloom layers for inference, forward and backward; announces oneself to the DHT"""

    def __init__(
        self,
        dht: DHT,
        module_backends: Dict[str, TransformerBackend],
        *,
        device: torch.device,
        num_connection_handlers: int = 8,
        update_period: float = 30,
        expiration: Optional[float] = None,
        start: bool,
        **kwargs,
    ):
        threading.Thread.__init__(self)
        self.dht, self.module_backends, self.update_period = dht, module_backends, update_period
        self.conn_handlers = [
            TransformerConnectionHandler(dht, self.module_backends) for _ in range(num_connection_handlers)
        ]
        self.runtime = Runtime(self.module_backends, device=device, **kwargs)
        self.dht_handler_thread = ModuleAnnouncerThread(
            self.module_backends, dht, update_period, expiration, daemon=True
        )
        self.checkpoint_saver = None  # no need to save checkpoints since we do not change model state

        if start:
            self.run_in_background(await_ready=True)

    def run(self):
        """
        Starts Server in the current thread. Initializes dht if necessary, starts connection handlers,
        runs Runtime (self.runtime) to process incoming requests.
        """
        logger.info(f"Serving {len(self.module_backends)} blocks:")
        for expert_name, backend in self.module_backends.items():
            num_parameters = sum(p.numel() for p in backend.module.parameters() if p.requires_grad)
            logger.info(f"{expert_name}: {backend.module.__class__.__name__}, {num_parameters} parameters")

        if not self.dht.is_alive():
            self.dht.run_in_background(await_ready=True)

        if self.module_backends:
            self.dht_handler_thread.start()

        if self.checkpoint_saver is not None:
            self.checkpoint_saver.start()

        for process in self.conn_handlers:
            if not process.is_alive():
                process.start()
            process.ready.result()

        try:
            self.runtime.run()
        finally:
            self.shutdown()

    # noinspection PyMethodOverriding
    @classmethod
    def create(
        cls,
        prefix: str,
        converted_model_name_or_path: str,
        num_blocks: Optional[int] = None,
        block_indices: Optional[str] = None,
        num_handlers: Optional[int] = None,
        min_batch_size: int = 1,
        max_batch_size: int = 4096,
        torch_dtype: str = "auto",
        cache_size_bytes: Optional[int] = None,
        device: Union[str, torch.device] = None,
        initial_peers: Sequence[str] = (),
        compression=CompressionType.NONE,
        stats_report_interval: Optional[int] = None,
        custom_module_path=None,
        update_period: float = 30,
        expiration: Optional[float] = None,
        use_auth_token: Optional[str] = None,
        *,
        start: bool,
        **kwargs,
    ) -> Server:
        """Create a server with one or more bloom blocks. See run_server.py for documentation."""
        if custom_module_path is not None:
            add_custom_models_from_file(custom_module_path)
        assert (block_indices is None) != (num_blocks is None), "please specify num_blocks or block_indices, not both"
        dht = DHT(initial_peers=initial_peers, start=True, **kwargs)
        visible_maddrs_str = [str(a) for a in dht.get_visible_maddrs()]
        logger.info(f"Running DHT node on {visible_maddrs_str}, initial peers = {initial_peers}")

        device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        memory_cache = MemoryCache(device, cache_size_bytes)

        if isinstance(torch_dtype, str):
            torch_dtype = DTYPE_MAP[torch_dtype]
        assert torch_dtype in DTYPE_MAP.values(), f"torch_dtype must be one of {list(DTYPE_MAP.values())}"

        if block_indices is not None:
            try:
                first_block_index, last_block_index = block_indices.split(":")
                first_block_index, last_block_index = map(int, map(str.strip, (first_block_index, last_block_index)))
            except Exception as e:
                logger.error(f"Failed to parse --block_indices ({e}), must be start:end (e.g. 0:18)")
                raise
            block_indices = range(first_block_index, last_block_index)
        else:
            assert num_blocks is not None
            block_indices = range(num_blocks)  # TODO replace with proper load balancing

        block_config = DistributedBloomConfig.from_pretrained(converted_model_name_or_path, use_auth_token=True)

        # initialize modules
        blocks = {}
        for block_index in block_indices:
            module_uid = f"{prefix}.{block_index}"
            block = load_pretrained_block(
                converted_model_name_or_path,
                block_index,
                block_config,
                torch_dtype=torch_dtype,
                use_auth_token=use_auth_token,
            )
            for param in block.parameters():
                param.requires_grad = False

            blocks[module_uid] = TransformerBackend(
                module_uid,
                block,
                memory_cache=memory_cache,
                args_schema=(BatchTensorDescriptor(1, 2048, block_config.hidden_size, compression=compression),),
                kwargs_schema={},
                outputs_schema=(BatchTensorDescriptor(1, 2048, block_config.hidden_size, compression=compression),),
                min_batch_size=min_batch_size,
                max_batch_size=max_batch_size,
            )

        num_handlers = num_handlers if num_handlers is not None else len(blocks) * 4

        return cls(
            dht,
            blocks,
            num_connection_handlers=num_handlers,
            device=device,
            stats_report_interval=stats_report_interval,
            update_period=update_period,
            expiration=expiration,
            start=start,
        )

    def run_in_background(self, await_ready=True, timeout=None):
        """
        Starts Server in a background thread. if await_ready, this method will wait until background server
        is ready to process incoming requests or for :timeout: seconds max.
        """
        self.start()
        if await_ready and not self.ready.wait(timeout=timeout):
            raise TimeoutError("Server didn't notify .ready in {timeout} seconds")

    @property
    def ready(self) -> mp.synchronize.Event:
        """
        An event (multiprocessing.Event) that is set when the server is ready to process requests.

        Example
        =======
        >>> server.start()
        >>> server.ready.wait(timeout=10)
        >>> print("Server ready" if server.ready.is_set() else "Server didn't start in 10 seconds")
        """
        return self.runtime.ready  # mp.Event that is true if self is ready to process batches

    def shutdown(self):
        """
        Gracefully terminate the server, process-safe.
        Please note that terminating server otherwise (e.g. by killing processes) may result in zombie processes.
        If you did already cause a zombie outbreak, your only option is to kill them with -9 (SIGKILL).
        """
        self.ready.clear()

        for process in self.conn_handlers:
            process.terminate()
            process.join()
        logger.debug("Connection handlers terminated")

        if self.module_backends:
            self.dht_handler_thread.stop.set()
            self.dht_handler_thread.join()

        if self.checkpoint_saver is not None:
            self.checkpoint_saver.stop.set()
            self.checkpoint_saver.join()

        self.dht.shutdown()
        self.dht.join()

        logger.debug(f"Shutting down runtime")

        self.runtime.shutdown()
        logger.info("Server shutdown succesfully")


class ModuleAnnouncerThread(threading.Thread):
    """Periodically announces that this server hosts the specified modules, visible to all DHT peers"""

    def __init__(
        self, module_backends, dht: DHT, update_period: float = 30, expiration: Optional[int] = None, **kwargs
    ):
        super().__init__(**kwargs)
        if expiration is None:
            expiration = max(2 * update_period, MAX_DHT_TIME_DISCREPANCY_SECONDS)
        self.module_backends = module_backends
        self.dht = dht
        self.update_period = update_period
        self.expiration = expiration
        self.stop = threading.Event()

    def run(self) -> None:
        declare_active_modules(self.dht, self.module_backends.keys(), get_dht_time() + self.expiration)
        while not self.stop.wait(self.update_period):
            declare_active_modules(self.dht, self.module_backends.keys(), get_dht_time() + self.expiration)
