# SPDX-License-Identifier: Apache-2.0
"""``lmcache bench l2`` subcommand implementation.

This module provides argument registration via :func:`add_l2_arguments`
and the execution orchestrator :func:`run_l2_adapter_bench` for the L2
adapter benchmark.
"""

# Future
from __future__ import annotations

# Standard
from typing import TYPE_CHECKING, Callable
import argparse
import os
import sys

if TYPE_CHECKING:
    # First Party
    from lmcache.cli.commands.base import BaseCommand
    from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchResult


def _noop_shutdown() -> None:
    """Stand-in for the metrics shutdown when the endpoint is off."""


def _parse_read_write_ratio(value: str) -> tuple[int, int]:
    """Parse a positive ``READ:WRITE`` ratio supplied on the command line."""
    parts = value.split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            "--read-write-ratio must use positive READ:WRITE integers, e.g. 5:1"
        )
    try:
        read_count, write_count = (int(part) for part in parts)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            "--read-write-ratio must use positive READ:WRITE integers, e.g. 5:1"
        ) from e
    if read_count <= 0 or write_count <= 0:
        raise argparse.ArgumentTypeError(
            "--read-write-ratio values must both be positive, e.g. 5:1"
        )
    return read_count, write_count


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_l2_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``lmcache bench l2`` arguments to *parser*.

    Args:
        parser: The ``ArgumentParser`` for the L2 bench subcommand.
    """

    parser.add_argument(
        "--l2-adapter",
        dest="l2_adapter",
        action="append",
        default=None,
        type=str,
        metavar="JSON",
        help=(
            'L2 adapter spec as JSON with a "type" field and adapter-'
            'specific configs, e.g. \'{"type":"fs","base_path":"/tmp/'
            "bench\"}'. If not provided, falls back to L2_ADAPTER_JSON "
            "environment variable."
        ),
    )
    parser.add_argument(
        "--num-keys",
        type=int,
        default=32,
        help="Keys per submit (default: 32).",
    )
    parser.add_argument(
        "--in-flight",
        type=int,
        default=1,
        help=(
            "In-flight submits per round. Each round issues this many "
            "submits sequentially from a single producer thread, then "
            "waits for all of them (default: 1)."
        ),
    )
    parser.add_argument(
        "--data-size-kb",
        type=int,
        default=256,
        help="Data size per key in KB (default: 256).",
    )
    parser.add_argument(
        "--l1-align-bytes",
        type=int,
        default=1,
        help=(
            "Alignment in bytes for benchmark L1 buffers. "
            "Use 4096 when benchmarking O_DIRECT backends. Default: 1."
        ),
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Measurement rounds per operation (default: 1).",
    )
    parser.add_argument(
        "--key-prefix",
        type=str,
        default="",
        help=(
            "Key namespace prefix, folded into the ObjectKey model_name. "
            "Keys are a pure function of this plus the key index, so two "
            "runs sharing a prefix address the same backing objects. That "
            "is what lets --only store be followed by --only load -- pass "
            "the SAME prefix to both. It also means a repeated STORE run "
            "re-targets objects that already exist, which backends that "
            "short-circuit an existing key report as success without "
            "writing, so pass a fresh prefix (a timestamp or run id) for "
            "an independent store measurement. REQUIRED for any run with "
            "a store phase, in either mode; concurrent producers each "
            "need a DISTINCT prefix. Default: empty, which addresses the "
            "historical unprefixed namespace and is usable for load and "
            "lookup runs without further flags."
        ),
    )
    parser.add_argument(
        "--write-key-prefix",
        type=str,
        default="",
        help=(
            "Distinct key namespace for stores in --read-write-ratio mode. "
            "The read corpus stays under --key-prefix while mixed stores "
            "advance monotonically in this namespace. Required and must "
            "differ from --key-prefix when mixed mode is selected."
        ),
    )
    parser.add_argument(
        "--read-write-ratio",
        type=_parse_read_write_ratio,
        default=None,
        metavar="READ:WRITE",
        help=(
            "Run one sustained mixed window with this requested positive "
            "payload ratio, e.g. 5:1. Requires --duration-sec, a "
            "prepopulated --key-prefix, and a distinct --write-key-prefix. "
            "Cannot be combined with --only."
        ),
    )
    parser.add_argument(
        "--unsafe-shared-key-prefix",
        action="store_true",
        help=(
            "Allow a store phase to run with no --key-prefix, writing "
            "into the shared unprefixed namespace. Unsafe: two such runs "
            "target identical keys, and the pre-flight existence probe "
            "reports state rather than reserving the keyspace, so "
            "concurrent producers can both see it empty and then collide. "
            "For reproducing historical unprefixed corpora only."
        ),
    )
    parser.add_argument(
        "--serve-metrics",
        type=int,
        default=0,
        metavar="PORT",
        help=(
            "Serve live benchmark progress as Prometheus metrics on PORT "
            "for the duration of the process. Exists to give a benchmark "
            "run a time axis that host-side counters (RDMA NIC, NVMe "
            "SMART, per-NUMA CPU) can be aligned against, instead of "
            "bracketing the run and diffing counters by hand. Metrics are "
            "computed at scrape time from the live results, so nothing is "
            "added to the submit path. A 1-15s scrape is far too coarse "
            "to attribute host CPU to a phase of a run -- the end-of-run "
            "summary remains the authoritative per-run figure. Rate "
            "queries need a window of at least 60s. Binds loopback only "
            "by default -- see --metrics-bind-address. Default: 0 (off)."
        ),
    )
    parser.add_argument(
        "--metrics-bind-address",
        type=str,
        default="127.0.0.1",
        metavar="ADDR",
        help=(
            "Interface for --serve-metrics to bind. Defaults to "
            "127.0.0.1, so an unauthenticated endpoint is not reachable "
            "off-box; pass 0.0.0.0 only when Prometheus scrapes the rig "
            "remotely. Default: 127.0.0.1."
        ),
    )
    parser.add_argument(
        "--warmup-rounds",
        type=int,
        default=1,
        help="Warmup rounds before measurement (default: 1).",
    )
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=0.0,
        help=(
            "Run a sustained window for this many seconds instead of "
            "fixed rounds. Keeps --in-flight submits outstanding for the "
            "whole window, issuing one replacement per completion, so the "
            "worker pool never drains at a round edge. Use this for a "
            "steady-state throughput number. The two directions treat the "
            "key space differently: LOADS wrap around the prepopulated "
            "space that --rounds sizes (rounds * in-flight * num-keys "
            "keys), so a long window re-reads keys the page cache may "
            "serve -- size it past DRAM or drop caches. STORES never "
            "wrap; they advance monotonically past that space so every "
            "submit is a physical write, consuming in-flight * num-keys * "
            "data-size bytes of backing capacity per completed wave for "
            "the whole window. Size the backing store for the duration. "
            "Lookup is unsupported in this mode. Default: 0 (rounds mode)."
        ),
    )
    parser.add_argument(
        "--warmup-sec",
        type=float,
        default=0.0,
        help=(
            "Discarded sustained window run before the measured one, in "
            "seconds. Only used with --duration-sec. Default: 0."
        ),
    )
    parser.add_argument(
        "--lookup-max-hit-rate",
        type=float,
        default=0.0,
        help=(
            "Upper bound on the lookup hit rate, in [0, 1]. The "
            "benchmark will request floor(N * rate) keys from the "
            "potentially-existing range and (N - hit) keys from a "
            "guaranteed-non-existent range, where N is the total "
            "number of lookup keys (rounds * in_flight * num_keys). "
            "The actual hit rate may be lower if those keys were "
            "never stored. Default: 0.0."
        ),
    )
    # Round-trip verification is OFF by default because it needs both
    # store and load object batches resident at the same time.
    # Use --no-skip-verify to enable verification.
    parser.add_argument(
        "--skip-verify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Skip round-trip data verification (default). "
            "Pass --no-skip-verify to enable verification."
        ),
    )
    parser.add_argument(
        "--only",
        choices=["lookup", "store", "load"],
        default=None,
        help="Run only the specified operation (default: run all).",
    )
    parser.add_argument(
        "--flamegraph",
        choices=["on", "off"],
        default="off",
        help=(
            "Capture a flame graph of the measured phases if turned on. The benchmark "
            "profiles itself and renders an SVG"
        ),
    )
    parser.add_argument(
        "--flamegraph-mode",
        default="on-cpu",
        metavar="MODE[,MODE...]",
        help=(
            "What to sample when profiling is on (default: on-cpu). Pass "
            "several comma-separated to profile one benchmark run per mode. "
            "Modes: on-cpu, off-cpu, wakeup, offwake (perf/bcc), wall, gil "
            "(py-spy). See the 'lmcache tool flamegraph' docs for detail."
        ),
    )
    parser.add_argument(
        "--flamegraph-output",
        default="",
        metavar="PATH",
        help=(
            "SVG output path for '--flamegraph on'. Default: "
            "/tmp/lmcache_bench_flames/<adapter>.<mode>.svg."
        ),
    )
    parser.add_argument(
        "--flamegraph-scripts-dir",
        default="",
        metavar="DIR",
        help=(
            "Directory with the FlameGraph scripts (flamegraph.pl, "
            "stackcollapse-perf.pl); default ~/FlameGraph (cloned there on "
            "first use). Unused by --flamegraph-mode wall / gil, which "
            "render their own SVG."
        ),
    )


# ---------------------------------------------------------------------------
# Core benchmark runner
# ---------------------------------------------------------------------------


def run_l2_adapter_bench(command: "BaseCommand", args: argparse.Namespace) -> None:
    """Run the L2 adapter benchmark.

    Args:
        command: The owning :class:`BaseCommand` instance, used only
            to obtain a configured :class:`Metrics` object via
            ``command.create_metrics``.
        args: Parsed CLI arguments from the ``bench l2`` subparser.
    """
    # Lazy imports: keep CLI loadable without torch / native deps.
    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.data import (
        create_l1_memory_desc,
        make_aligned_tensor,
        make_memory_objects,
        make_object_keys,
        verify_round_trip,
    )
    from lmcache.cli.commands.bench.l2_adapter_bench.runner import (
        bench_load,
        bench_load_sustained,
        bench_lookup,
        bench_mixed_sustained,
        bench_store,
        bench_store_sustained,
        StoreFreshnessUnknownError,
        StoreNamespaceNotEmptyError,
        require_empty_store_namespace,
    )
    from lmcache.cli.commands.bench.l2_adapter_bench.metrics import (
        PHASE_MEASURED,
        PHASE_WARMUP,
        PHASE_WARMUP_AND_MEASURED,
        BenchMetricsState,
        MetricsServerError,
        start_metrics_server,
    )
    from lmcache.cli.profiling import (
        PY_SPY_MODES,
        FlameProfiler,
        ProfileError,
        check_profiling_deps,
        default_output_path,
        resolve_flamegraph_dir,
    )
    from lmcache.v1.distributed.l2_adapters import create_l2_adapter
    from lmcache.v1.distributed.l2_adapters.config import (
        parse_args_to_l2_adapters_config,
    )

    kb = 1024
    mb = 1024 * 1024
    data_size = args.data_size_kb * kb
    l1_align_bytes = int(args.l1_align_bytes)
    if l1_align_bytes <= 0:
        print("Error: --l1-align-bytes must be positive", file=sys.stderr)
        sys.exit(2)
    if data_size % l1_align_bytes != 0:
        print(
            "Error: --data-size-kb must produce a payload size that is "
            "a multiple of --l1-align-bytes",
            file=sys.stderr,
        )
        sys.exit(2)
    in_flight = args.in_flight
    num_keys = args.num_keys
    rounds = args.rounds
    warmup = args.warmup_rounds
    total_rounds = warmup + rounds
    max_hit_rate = max(0.0, min(1.0, args.lookup_max_hit_rate))
    quiet = getattr(args, "quiet", False)
    duration_sec = float(getattr(args, "duration_sec", 0.0))
    warmup_sec = float(getattr(args, "warmup_sec", 0.0))
    sustained = duration_sec > 0
    read_write_ratio = getattr(args, "read_write_ratio", None)
    mixed = read_write_ratio is not None
    if duration_sec < 0:
        print("Error: --duration-sec must not be negative", file=sys.stderr)
        sys.exit(2)
    if warmup_sec < 0:
        print("Error: --warmup-sec must not be negative", file=sys.stderr)
        sys.exit(2)
    if warmup_sec > 0 and not sustained:
        print(
            "Error: --warmup-sec applies only to sustained mode; pass "
            "--duration-sec too, or use --warmup-rounds for rounds mode.",
            file=sys.stderr,
        )
        sys.exit(2)
    if mixed and not sustained:
        print(
            "Error: --read-write-ratio requires --duration-sec",
            file=sys.stderr,
        )
        sys.exit(2)
    if mixed and args.only is not None:
        print(
            "Error: --read-write-ratio cannot be combined with --only",
            file=sys.stderr,
        )
        sys.exit(2)
    if mixed and warmup_sec > 0:
        print(
            "Error: --warmup-sec is not supported with --read-write-ratio; "
            "it would issue unaccounted stores",
            file=sys.stderr,
        )
        sys.exit(2)
    if mixed and not args.key_prefix:
        print(
            "Error: --read-write-ratio requires --key-prefix for the "
            "prepopulated read corpus",
            file=sys.stderr,
        )
        sys.exit(2)
    write_key_prefix = str(getattr(args, "write_key_prefix", ""))
    if mixed and not write_key_prefix:
        print(
            "Error: --read-write-ratio requires --write-key-prefix for "
            "monotonic stores",
            file=sys.stderr,
        )
        sys.exit(2)
    if mixed and args.key_prefix == write_key_prefix:
        print(
            "Error: --write-key-prefix must differ from --key-prefix in mixed mode",
            file=sys.stderr,
        )
        sys.exit(2)
    if sustained and args.only == "lookup":
        print(
            "Error: --duration-sec does not support --only lookup",
            file=sys.stderr,
        )
        sys.exit(2)
    metrics_port = int(getattr(args, "serve_metrics", 0))
    metrics_address = str(getattr(args, "metrics_bind_address", "127.0.0.1"))
    if metrics_port and not (1 <= metrics_port <= 65535):
        print(
            "Error: --serve-metrics must be a TCP port in 1..65535",
            file=sys.stderr,
        )
        sys.exit(2)
    stores = args.only != "load" and args.only != "lookup"
    if stores and not args.key_prefix and not args.unsafe_shared_key_prefix:
        # Every store-containing run needs its own key universe, in both
        # modes. The pre-flight probe is not a reservation: two processes
        # can both find the default namespace empty and then write the
        # same keys, and backends that short-circuit an existing key
        # report success without writing. A distinct prefix per producer
        # is the only thing that actually keeps them disjoint. The
        # matching load pass must be given the same value.
        print(
            "Error: a store phase requires --key-prefix. Keys are a pure "
            "function of the prefix and the key index, so every store run "
            "needs its own namespace -- name this one explicitly (e.g. "
            "--key-prefix run-$(date +%s)) and pass the same prefix to "
            "the matching --only load pass. Concurrent producers each "
            "need a DISTINCT prefix; the pre-flight existence probe "
            "reports state, it does not reserve the keyspace. To write "
            "into the historical unprefixed namespace anyway, pass "
            "--unsafe-shared-key-prefix.",
            file=sys.stderr,
        )
        sys.exit(2)
    if sustained and not args.skip_verify:
        # Sustained mode recycles buffers across submits and does not
        # zero load buffers, so the round-trip comparison has no stable
        # pair to check. Fail rather than silently skip the gate.
        print(
            "Error: --no-skip-verify requires rounds mode; "
            "--duration-sec cannot verify round-trip integrity",
            file=sys.stderr,
        )
        sys.exit(2)
    if not args.skip_verify and args.only is not None:
        # The verify gate compares store source buffers against load
        # destination buffers, so it structurally needs both directions
        # in one process. With --only it could never run, and previously
        # did so silently -- a prepopulate + `--only load` split looked
        # verified while checking nothing.
        print(
            f"Error: --no-skip-verify needs both store and load in one run, "
            f"but --only {args.only} was requested. Drop --only, or drop "
            f"--no-skip-verify and rely on counter validation.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Keys per round (one in-flight wave) and total keys available to a
    # wrapping sustained load. Mixed mode has no round warmup: its
    # --warmup-rounds value is ignored so the parser default cannot make a
    # corpus prepopulated with --warmup-rounds 0 miss after its first pass.
    keys_per_round = in_flight * num_keys
    total_run_keys = (rounds if mixed else total_rounds) * keys_per_round
    # ``--key-prefix`` becomes part of the ObjectKey model_name, so it
    # partitions the key universe. Empty prefix keeps the historical
    # "bench-model" name, so existing rounds-mode corpora stay addressable.
    key_prefix = args.key_prefix
    key_namespace = f"{key_prefix}-bench-model" if key_prefix else "bench-model"
    write_key_namespace = f"{write_key_prefix}-bench-model" if mixed else key_namespace

    def log(msg: str) -> None:
        # Per-round progress log; suppressed by --quiet.
        if not quiet:
            print(msg)

    # Resolve L2 adapter JSON: CLI arg takes priority, then env var
    l2_adapter_specs = args.l2_adapter
    if not l2_adapter_specs:
        env_json = os.environ.get("L2_ADAPTER_JSON")
        if env_json:
            l2_adapter_specs = [env_json]
        else:
            print(
                "Error: No L2 adapter configuration provided.\n"
                "Use --l2-adapter JSON or set L2_ADAPTER_JSON "
                "environment variable.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Parse adapter config using the standard LMCache mechanism
    ns = argparse.Namespace(l2_adapter=l2_adapter_specs)
    try:
        l2_cfg = parse_args_to_l2_adapters_config(ns)
    except (ValueError, KeyError) as e:
        print(f"Error parsing L2 adapter config: {e}", file=sys.stderr)
        sys.exit(2)

    if not l2_cfg.adapters:
        print("Error: no L2 adapter configs parsed", file=sys.stderr)
        sys.exit(2)

    # Use the first adapter config for benchmarking
    adapter_cfg = l2_cfg.adapters[0]

    # Backing L1 memory buffer for adapters that need an L1 desc.
    # Sized for one in-flight wave of store + load buffers.
    l1_buffer = make_aligned_tensor(2 * keys_per_round * data_size, l1_align_bytes)
    l1_memory_desc = create_l1_memory_desc(l1_buffer, align_bytes=l1_align_bytes)

    # Resolve and validate the flame-graph toolchain up front, before any
    # adapter (and its worker threads) is created. A user who explicitly
    # passes ``--flamegraph on`` gets a fast, actionable failure if a
    # required tool is missing, rather than a benchmark that silently runs
    # unprofiled. When ``--flamegraph`` is off (default), none of this runs
    # and the benchmark behaves exactly as before.
    flamegraph_on = getattr(args, "flamegraph", "off") == "on"
    flamegraph_dir = ""
    if flamegraph_on:
        try:
            check_profiling_deps(args.flamegraph_mode)
            # py-spy renders its own SVG; only the perf/bcc modes need
            # the FlameGraph scripts, so do not clone them otherwise.
            if args.flamegraph_mode not in PY_SPY_MODES:
                flamegraph_dir = resolve_flamegraph_dir(
                    args.flamegraph_scripts_dir, log
                )
        except ProfileError as e:
            print(
                "Error: --flamegraph on was requested but the profiling "
                f"toolchain is unavailable:\n  {e}",
                file=sys.stderr,
            )
            sys.exit(2)

    # Bind the endpoint before the adapter exists, so a port clash fails
    # while there is still nothing to clean up. Doing it the other way
    # round leaks the adapter's worker threads: the exit below runs
    # outside the try/finally that closes it. The mirror obligation is
    # that every init failure between here and that try/finally must call
    # ``stop_metrics`` itself, or it leaks the listener instead.
    metrics_state = BenchMetricsState()
    stop_metrics: Callable[[], None] = _noop_shutdown
    if metrics_port:
        try:
            stop_metrics = start_metrics_server(
                metrics_port, metrics_state, address=metrics_address
            )
        except MetricsServerError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)
        log(
            f"[Metrics] Serving Prometheus metrics on "
            f"{metrics_address}:{metrics_port}/metrics"
        )

    log("\n[Init] Creating adapter...")
    try:
        adapter = create_l2_adapter(adapter_cfg, l1_memory_desc=l1_memory_desc)
        log(f"[Init] Adapter created successfully ({type(adapter).__name__}).\n")
    except Exception as e:
        print(f"[Init] Failed to create adapter: {e}", file=sys.stderr)
        stop_metrics()
        sys.exit(1)

    # Optional self-profiling: record a flame graph of the measured
    # phases. Built here, after the adapter (and its worker threads)
    # exist, so the recorder attaches to a fully-started process. The
    # toolchain was already validated above, so a ProfileError here is a
    # late failure (e.g. the output directory is not writable); since the
    # user explicitly requested a flame graph we fail loudly rather than
    # downgrade, closing the adapter we just opened first.
    profiler: FlameProfiler | None = None
    if flamegraph_on:
        adapter_name = type(adapter).__name__
        try:
            profiler = FlameProfiler(
                mode=args.flamegraph_mode,
                output=(
                    args.flamegraph_output
                    or default_output_path(adapter_name, args.flamegraph_mode)
                ),
                flamegraph_dir=flamegraph_dir,
                pid=os.getpid(),
                title=f"{args.flamegraph_mode} ({adapter_name})",
            )
        except ProfileError as e:
            print(
                f"Error: cannot start flame-graph profiling: {e}",
                file=sys.stderr,
            )
            try:
                adapter.close()
            except Exception:
                pass
            stop_metrics()
            sys.exit(2)

    # ------------------------------------------------------------------
    # Idx layout
    # ------------------------------------------------------------------
    # All ops live in the same idx universe so that ``--only store``
    # followed by ``--only load`` (or lookup) with the same flags hits
    # the exact same keys.
    #
    # Round r (0-indexed, warmup rounds first) consumes the idx slice
    #   [r * keys_per_round, (r+1) * keys_per_round)
    # split into ``in_flight`` contiguous batches of ``num_keys`` each.
    #
    # Lookup additionally splits each round into a hit-portion (drawn
    # from the same idx range as store/load) and a miss-portion drawn
    # from a guaranteed-non-existent range starting at
    # ``total_run_keys``.
    # ------------------------------------------------------------------

    def _build_round_keys(r: int) -> list[list]:
        """Build per-submit key batches for round *r* (store/load)."""
        base = r * keys_per_round
        return [
            make_object_keys(
                num_keys,
                model_name=key_namespace,
                key_offset=base + i * num_keys,
            )
            for i in range(in_flight)
        ]

    def _build_round_objs(base_offset: int, fill_offset: int = 0) -> list[list]:
        """Build per-submit object batches backed by the registered L1 buffer.

        Some adapters register the L1 buffer passed through ``L1MemoryDesc``
        during initialization. The benchmark objects must therefore be views
        into that same buffer rather than independent tensors allocated
        elsewhere.

        ``fill_offset`` lets load buffers start with a pattern that differs
        from store buffers, so round-trip verification catches silent no-op
        loads that nevertheless report success.
        """
        return [
            make_memory_objects(
                l1_buffer,
                num_keys,
                data_size,
                base_offset + i * num_keys * data_size,
                fill_offset=fill_offset,
            )
            for i in range(in_flight)
        ]

    # Lookup hit/miss split per round.
    per_round_hit = int(keys_per_round * max_hit_rate)
    per_round_miss = keys_per_round - per_round_hit
    # Total expected hit count over measured rounds only.
    expected_hit_count = per_round_hit * rounds
    # Origin of the guaranteed-miss idx range.
    miss_origin = total_run_keys

    def _build_lookup_round_keys(r: int) -> list[list]:
        """Build per-submit lookup key batches for round *r*.

        Hit slice for round r:
          [r * per_round_hit, (r+1) * per_round_hit)
        Miss slice for round r (disjoint from any store/load idx):
          [miss_origin + r * per_round_miss,
           miss_origin + (r+1) * per_round_miss)

        The combined ``keys_per_round`` keys are concatenated then
        split into ``in_flight`` chunks of ``num_keys`` each.
        """
        hit_base = r * per_round_hit
        miss_base = miss_origin + r * per_round_miss
        keys_round: list = []
        keys_round.extend(
            make_object_keys(
                per_round_hit, model_name=key_namespace, key_offset=hit_base
            )
        )
        keys_round.extend(
            make_object_keys(
                per_round_miss, model_name=key_namespace, key_offset=miss_base
            )
        )
        # Split into in_flight equal-sized batches of num_keys.
        return [keys_round[i * num_keys : (i + 1) * num_keys] for i in range(in_flight)]

    # Per-direction object batches for store / load.
    #
    # Allocation strategy:
    # * Lazy: only allocate when the corresponding direction is
    #   actually exercised. With ``--only store`` we never touch
    #   load buffers (and vice versa), saving
    #   ``in_flight * num_keys * data_size`` bytes of host memory.
    # * Cross-round reuse: once allocated, the same batches are
    #   fed into every round; only the keys change per round. The
    #   L2 adapter does not care about object identity across
    #   rounds, and re-allocating these buffers each round would
    #   just be wasted work.
    store_obj_batches: list[list] | None = None
    load_obj_batches: list[list] | None = None

    def _store_objs(_r: int) -> list[list]:
        nonlocal store_obj_batches
        if store_obj_batches is None:
            store_obj_batches = _build_round_objs(0)
        return store_obj_batches

    def _load_objs(_r: int) -> list[list]:
        nonlocal load_obj_batches
        if load_obj_batches is None:
            load_obj_batches = _build_round_objs(
                keys_per_round * data_size,
                fill_offset=1,
            )
        return load_obj_batches

    # ------------------------------------------------------------------
    # Sustained-mode providers
    # ------------------------------------------------------------------
    # A sustained window issues an unbounded number of submits, so the
    # submit index -> key idx mapping has to be defined past the end of
    # the rounds-mode key universe (``total_run_keys`` keys). The two
    # directions need opposite treatment:
    #
    # Store MUST NOT wrap. ``fs_native`` short-circuits a store whose key
    # already exists and reports success without writing anything
    # (csrc/storage_backends/fs/connector.cpp, ``do_single_set``). A
    # wrapped store window would therefore measure the existence check,
    # not the write path, and still count full payload bytes. Store keys
    # advance monotonically so every submit is a physical write. The cost
    # is unbounded capacity growth: a sustained store consumes
    # ``in_flight * num_keys * data_size`` bytes per completed wave for
    # the whole window, so size the backing store for the duration.
    #
    # Load MUST wrap: it can only hit keys that were actually stored, so
    # it stays inside ``total_run_keys``. A window long enough to wrap
    # re-reads keys, which the page cache may serve -- size the key space
    # past DRAM or drop caches between phases.
    #
    # Window *slots* map to the per-submit batches rounds mode already
    # allocates: slot i owns batch i. A slot is only reissued after its
    # previous submit completed, so no two outstanding submits share
    # buffers.
    total_submit_slots = max(1, total_run_keys // num_keys)

    def _sustained_store_keys(submit_index: int) -> list:
        """Keys for sustained store submit *submit_index*.

        Monotonic, never wrapping, so no submit can land on an
        already-stored key and degenerate into a no-op success. Note this
        holds only *within* one invocation -- across invocations the
        offset restarts at zero, which is what ``--key-prefix``
        guards.
        """
        return make_object_keys(
            num_keys,
            model_name=write_key_namespace,
            key_offset=submit_index * num_keys,
        )

    def _sustained_load_keys(submit_index: int) -> list:
        """Keys for sustained load submit *submit_index* (wraps).

        Wraps within ``total_run_keys`` -- the idx range a prepopulating
        store pass at matching geometry actually covered -- so reads hit
        rather than measuring the miss path. Mixed mode intentionally uses
        measured rounds only because it has no rounds warmup.
        """
        slot_idx = submit_index % total_submit_slots
        return make_object_keys(
            num_keys,
            model_name=key_namespace,
            key_offset=slot_idx * num_keys,
        )

    def _first_store_wave_keys() -> list:
        """Keys the store phase writes first, in whichever mode is active.

        Both modes start at key index 0, so the two branches agree today;
        they are kept distinct so a future change to either key provider
        cannot silently make the probe test the wrong keys.
        """
        if sustained:
            return _sustained_store_keys(0)
        return [k for batch in _build_round_keys(0) for k in batch]

    def _sustained_store_objs(slot: int) -> list:
        return _store_objs(0)[slot]

    def _sustained_load_objs(slot: int) -> list:
        return _load_objs(0)[slot]

    # Rounds mode drives warmup and measured rounds through one result, so
    # the live series unavoidably carries both -- ``_strip_warmup`` only
    # separates them afterwards, when the summary is computed. Label it for
    # what it is rather than letting it pass as measured-only.
    rounds_phase = PHASE_WARMUP_AND_MEASURED if warmup else PHASE_MEASURED

    def _publish_measured(result) -> None:
        """Register a measured phase's live result with the endpoint."""
        metrics_state.register(result.operation, result, PHASE_MEASURED)

    def _publish_warmup(result) -> None:
        """Register a sustained phase's discarded warmup window.

        A separate series, so it can never be summed into the measured
        figures, but still exposed: the NIC and NVMe counters this
        endpoint exists to align against do include warmup I/O.
        """
        metrics_state.register(result.operation, result, PHASE_WARMUP)

    def _publish_rounds(result) -> None:
        """Register a rounds-mode result under :data:`rounds_phase`."""
        metrics_state.register(result.operation, result, rounds_phase)

    results: list = []
    failed = False
    failed_precondition = False

    # Track the very last measured store round so we can verify it
    # against the matching load round (round-trip integrity check).
    last_store_round_keys: list[list] | None = None
    last_load_round_keys: list[list] | None = None

    try:
        if profiler is not None:
            # Pre-build the payload buffers before the recorder starts so
            # the one-time tensor allocation + fill (benchmark harness work,
            # not the adapter) is kept out of the flame graph. The batches
            # are reused across rounds, so this is the only build; warming
            # it here keeps the recording focused on adapter I/O.
            if args.only is None or args.only == "store":
                _store_objs(0)
            if args.only is None or args.only == "load":
                _load_objs(0)
            profiler.start(log)

        if mixed:
            # Stores use their own prefix. The existing first-wave guard is
            # enough for this benchmark's fresh, driver-supplied namespace;
            # capacity and lifecycle policy remain outside the harness.
            require_empty_store_namespace(
                adapter,
                keys=_sustained_store_keys(0),
                namespace=write_key_namespace,
                log=log,
            )
            load_result, store_result, accepted = bench_mixed_sustained(
                adapter,
                in_flight=in_flight,
                num_keys=num_keys,
                data_size=data_size,
                duration_sec=duration_sec,
                read_write_ratio=read_write_ratio,
                load_keys_for_submit=_sustained_load_keys,
                store_keys_for_submit=_sustained_store_keys,
                load_objs_for_slot=_sustained_load_objs,
                store_objs_for_slot=_sustained_store_objs,
                log=log,
                on_result=_publish_measured,
            )
            results.extend([load_result, store_result])
            failed = not accepted
            log("")

        # ---- Store ----
        if not mixed and (args.only is None or args.only == "store"):
            # Probe before writing anything: the first wave's keys are
            # enough to tell whether this namespace was already used at
            # this geometry. A hit means the run would measure existence
            # checks while counting full payload bytes.
            require_empty_store_namespace(
                adapter,
                keys=_first_store_wave_keys(),
                namespace=key_namespace,
                log=log,
            )
            if sustained:
                results.append(
                    bench_store_sustained(
                        adapter,
                        in_flight=in_flight,
                        num_keys=num_keys,
                        data_size=data_size,
                        duration_sec=duration_sec,
                        warmup_sec=warmup_sec,
                        keys_for_submit=_sustained_store_keys,
                        objs_for_slot=_sustained_store_objs,
                        log=log,
                        on_result=_publish_measured,
                        on_warmup_result=_publish_warmup,
                    )
                )
            else:
                log(f"[Store] Running {warmup} warmup + {rounds} measurement rounds...")
                all_store = bench_store(
                    adapter,
                    in_flight=in_flight,
                    num_keys=num_keys,
                    data_size=data_size,
                    rounds=total_rounds,
                    keys_for_round=_build_round_keys,
                    objs_for_round=_store_objs,
                    log=log,
                    on_result=_publish_rounds,
                )
                results.append(_strip_warmup(all_store, warmup))
                # Last measured store round is total_rounds - 1.
                last_store_round_keys = _build_round_keys(total_rounds - 1)
            log("")

        # ---- Lookup ----
        # Skipped entirely in sustained mode: mixing a rounds-mode lookup
        # into a sustained run would put two incomparable measurement
        # modes in one report. ``--only lookup`` with --duration-sec is
        # rejected up front.
        if not mixed and not sustained and (args.only is None or args.only == "lookup"):
            log(f"[Lookup] Running {warmup} warmup + {rounds} measurement rounds...")
            all_lookup = bench_lookup(
                adapter,
                in_flight=in_flight,
                num_keys=num_keys,
                rounds=total_rounds,
                keys_for_round=_build_lookup_round_keys,
                log=log,
                expected_max_hit_rate=max_hit_rate,
                expected_hit_count=expected_hit_count,
                on_result=_publish_rounds,
            )
            results.append(_strip_warmup(all_lookup, warmup))
            log("")

        # ---- Load ----
        if not mixed and (args.only is None or args.only == "load"):
            if sustained:
                results.append(
                    bench_load_sustained(
                        adapter,
                        in_flight=in_flight,
                        num_keys=num_keys,
                        data_size=data_size,
                        duration_sec=duration_sec,
                        warmup_sec=warmup_sec,
                        keys_for_submit=_sustained_load_keys,
                        objs_for_slot=_sustained_load_objs,
                        log=log,
                        on_result=_publish_measured,
                        on_warmup_result=_publish_warmup,
                    )
                )
            else:
                log(f"[Load] Running {warmup} warmup + {rounds} measurement rounds...")
                all_load = bench_load(
                    adapter,
                    in_flight=in_flight,
                    num_keys=num_keys,
                    data_size=data_size,
                    rounds=total_rounds,
                    keys_for_round=_build_round_keys,
                    objs_for_round=_load_objs,
                    log=log,
                    on_result=_publish_rounds,
                )
                results.append(_strip_warmup(all_load, warmup))
                last_load_round_keys = _build_round_keys(total_rounds - 1)
            log("")

        # Stop profiling before verification / summary so the flame
        # graph reflects only the measured store/lookup/load work.
        if profiler is not None:
            profiler.stop(log)

        # ---- Round-trip verification (last measured round only) ----
        if (
            not args.skip_verify
            and store_obj_batches is not None
            and load_obj_batches is not None
            and last_store_round_keys is not None
            and last_load_round_keys is not None
        ):
            # Sanity: store and load used the same key idx range for
            # the last measured round, and load buffers now hold what
            # the adapter returned. Compare against the byte pattern
            # written by the store object batch (i & 0xFF, where i is
            # position within the batch).
            log(
                "[Verify] Checking store -> load data integrity for last "
                "measured round..."
            )
            flat_keys = [k for kl in last_load_round_keys for k in kl]
            flat_store = [o for ol in store_obj_batches for o in ol]
            flat_load = [o for ol in load_obj_batches for o in ol]
            ok = verify_round_trip(flat_keys, flat_store, flat_load, log)
            if not ok:
                failed = True
            log("")

        # ---- Summary via metrics system ----
        _emit_l2_adapter_metrics(
            command=command,
            args=args,
            l2_adapter_json=l2_adapter_specs[0],
            keys_per_round=keys_per_round,
            data_per_round_mb=(keys_per_round * data_size) / mb,
            results=results,
        )
    except (StoreNamespaceNotEmptyError, StoreFreshnessUnknownError) as e:
        # A usage error, not a benchmark failure: nothing was measured, so
        # exit 2 like the argument-validation paths rather than 1. Both an
        # occupied namespace and an unanswerable probe land here -- the
        # gate fails closed either way.
        print(f"Error: {e}", file=sys.stderr)
        failed_precondition = True
    finally:
        # Idempotent: a no-op if profiling already stopped on the normal
        # path; tears the recorder down if a phase raised.
        if profiler is not None:
            profiler.stop(log)
        log("[Cleanup] Closing adapter...")
        try:
            adapter.close()
        except Exception as e:
            print(f"[Cleanup] adapter.close() failed: {e}", file=sys.stderr)
        # Closed last, after the summary is printed. The endpoint dies
        # with the run, so the final scrape interval is truncated: read
        # the summary table, not the tail of the rate() curve, for the
        # last few seconds. Releasing the socket matters for in-process
        # callers -- otherwise each run leaks a listener for the life of
        # the interpreter.
        if metrics_port:
            log("[Cleanup] Stopping metrics endpoint...")
            stop_metrics()
        log("[Cleanup] Done.")

    if failed_precondition:
        sys.exit(2)
    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_warmup(result: "BenchResult", warmup: int) -> "BenchResult":
    """Drop the leading *warmup* rounds from a BenchResult."""
    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchResult

    # Adjust the expected hit count proportionally for the kept rounds.
    kept_rounds = max(0, len(result.round_durations) - warmup)
    total_rounds = max(1, len(result.round_durations))
    scaled_expected_hit = int(result.expected_hit_count * kept_rounds / total_rounds)

    # Per-submit latencies are recorded in submit order, so the warmup
    # prefix is the sum of the per-round counts (which is in_flight per
    # round unless a round timed out).
    dropped_submits = sum(result.round_latency_counts[:warmup])

    return BenchResult(
        operation=result.operation,
        in_flight=result.in_flight,
        num_keys=result.num_keys,
        data_size_bytes=result.data_size_bytes,
        mode=result.mode,
        round_durations=result.round_durations[warmup:],
        round_starts=result.round_starts[warmup:],
        success_counts=result.success_counts[warmup:],
        submit_latencies=result.submit_latencies[dropped_submits:],
        round_latency_counts=result.round_latency_counts[warmup:],
        # Kept consistent with the surviving rounds; dropped_submits is
        # exactly the warmup prefix's contribution.
        completed_submits=result.completed_submits - dropped_submits,
        timed_out=result.timed_out,
        expected_max_hit_rate=result.expected_max_hit_rate,
        expected_hit_count=scaled_expected_hit,
    )


def _emit_l2_adapter_metrics(
    command: "BaseCommand",
    args: argparse.Namespace,
    l2_adapter_json: str,
    keys_per_round: int,
    data_per_round_mb: float,
    results: list,
) -> None:
    """Emit L2 adapter benchmark summary using the CLI metrics system."""
    title = "L2 Adapter Benchmark Result"
    metrics = command.create_metrics(title, args, width=64)

    cfg_section = metrics.add_section("config", "Configuration")
    cfg_section.add("l2_adapter_json", "L2 adapter JSON", l2_adapter_json)
    cfg_section.add("num_keys", "Keys / submit", args.num_keys)
    cfg_section.add("in_flight", "In-flight / round", args.in_flight)
    cfg_section.add("keys_per_round", "Keys / round", keys_per_round)
    cfg_section.add(
        "data_size_kb",
        "Data size / key (KB)",
        args.data_size_kb,
    )
    cfg_section.add(
        "data_per_round_mb",
        "Data / round (MB)",
        round(data_per_round_mb, 2),
    )
    duration_sec = float(getattr(args, "duration_sec", 0.0))
    if duration_sec > 0:
        cfg_section.add("mode", "Measurement mode", "sustained")
        cfg_section.add("duration_sec", "Window (s)", round(duration_sec, 3))
        cfg_section.add(
            "warmup_sec",
            "Warmup window (s)",
            round(float(getattr(args, "warmup_sec", 0.0)), 3),
        )
        read_write_ratio = getattr(args, "read_write_ratio", None)
        if read_write_ratio is not None:
            read_count, write_count = read_write_ratio
            cfg_section.add(
                "read_write_ratio_requested",
                "Requested read:write",
                f"{read_count}:{write_count}",
            )
            cfg_section.add(
                "write_key_prefix",
                "Write key prefix",
                getattr(args, "write_key_prefix", ""),
            )
    else:
        cfg_section.add("mode", "Measurement mode", "rounds")
        cfg_section.add("measurement_rounds", "Measurement rounds", args.rounds)
        cfg_section.add("warmup_rounds", "Warmup rounds", args.warmup_rounds)
    # Only meaningful when lookup is actually executed; matches the
    # original banner log behaviour. Sustained mode never runs lookup.
    if duration_sec <= 0 and (args.only is None or args.only == "lookup"):
        cfg_section.add(
            "lookup_max_hit_rate",
            "Lookup max hit rate",
            round(args.lookup_max_hit_rate, 4),
        )

    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchMode

    for idx, r in enumerate(results):
        section_id = f"op_{idx}"
        section = metrics.add_section(section_id, r.operation)
        section.add("operation", "Operation", r.operation)
        sustained_result = r.mode is BenchMode.SUSTAINED
        if sustained_result:
            section.add("submits", "Submits completed", r.completed_submits)
            section.add(
                "window_sec",
                "Measured window (s)",
                round(r.sustained_window_sec, 3),
            )
            section.add(
                "drain_tail_sec",
                "Ramp-down tail (s)",
                round(r.sustained_drain_sec, 3),
            )
        else:
            section.add("rounds", "Rounds", len(r.round_durations))
            section.add("keys_per_round", "Keys / round", r.keys_per_round)
        section.add("total_keys", "Total keys", r.total_keys)
        section.add("total_success", "Total success", r.total_success)
        if r.timed_out:
            section.add("timed_out", "Timed out", True)
        if not sustained_result:
            section.add(
                "duration_avg_ms",
                "Duration avg (ms)",
                round(r.avg_duration * 1000, 2),
            )
            section.add(
                "duration_min_ms",
                "Duration min (ms)",
                round(r.min_duration * 1000, 2),
            )
            section.add(
                "duration_max_ms",
                "Duration max (ms)",
                round(r.max_duration * 1000, 2),
            )
            section.add(
                "duration_p50_ms",
                "Duration p50 (ms)",
                round(r.p50_duration * 1000, 2),
            )
            section.add(
                "duration_p99_ms",
                "Duration p99 (ms)",
                round(r.p99_duration * 1000, 2),
            )
            section.add(
                "duration_std_ms",
                "Duration std (ms)",
                round(r.std_duration * 1000, 2),
            )
        # Per-submit latency distribution. Unlike the duration_* fields
        # above (which are percentiles over whole rounds) these are per
        # submit, so a single straggler is distinguishable from a
        # uniformly slow round. Upper bound on service time -- see
        # BenchResult.submit_latencies.
        if r.submit_count > 0:
            section.add("submit_latency_count", "Submits measured", r.submit_count)
            if r.submit_latency_sample_count != r.submit_count:
                section.add(
                    "submit_latency_sample_count",
                    "Latency samples retained",
                    r.submit_latency_sample_count,
                )
            section.add(
                "submit_latency_avg_ms",
                "Submit latency avg (ms)",
                round(r.submit_latency_avg_ms, 3),
            )
            section.add(
                "submit_latency_min_ms",
                "Submit latency min (ms)",
                round(r.submit_latency_min_ms, 3),
            )
            section.add(
                "submit_latency_p50_ms",
                "Submit latency p50 (ms)",
                round(r.submit_latency_p50_ms, 3),
            )
            section.add(
                "submit_latency_p90_ms",
                "Submit latency p90 (ms)",
                round(r.submit_latency_p90_ms, 3),
            )
            section.add(
                "submit_latency_p99_ms",
                "Submit latency p99 (ms)",
                round(r.submit_latency_p99_ms, 3),
            )
            section.add(
                "submit_latency_max_ms",
                "Submit latency max (ms)",
                round(r.submit_latency_max_ms, 3),
            )
        # Aggregate throughput: requested payload / total measured time.
        # Preferred over throughput_avg, which is a mean of per-round
        # rates and over-weights fast rounds.
        section.add(
            "throughput_aggregate_mbps",
            "Throughput aggregate (MB/s)",
            round(r.aggregate_throughput_mbps, 2),
        )
        # Successful-bytes throughput. Emitted whenever it diverges from
        # the requested figure, which means keys were missed or a store
        # was short-circuited -- the fio comparator wants this one.
        if r.data_size_bytes > 0 and r.total_success != r.total_keys:
            section.add(
                "throughput_success_mbps",
                "Throughput successful (MB/s)",
                round(r.success_throughput_mbps, 2),
            )
        if not sustained_result:
            # Charges the run for the inter-round gaps the timed regions
            # exclude. Zero when round starts were not recorded.
            if r.wall_clock_throughput_mbps > 0:
                section.add(
                    "throughput_wall_clock_mbps",
                    "Throughput wall clock (MB/s)",
                    round(r.wall_clock_throughput_mbps, 2),
                )
                section.add(
                    "barrier_idle_pct",
                    "Round-edge idle (%)",
                    round(r.barrier_idle_fraction * 100, 2),
                )
            section.add(
                "throughput_avg_mbps",
                "Throughput avg (MB/s)",
                round(r.avg_throughput_mbps, 2),
            )
            section.add(
                "throughput_min_mbps",
                "Throughput min (MB/s)",
                round(r.min_throughput_mbps, 2),
            )
            section.add(
                "throughput_max_mbps",
                "Throughput max (MB/s)",
                round(r.max_throughput_mbps, 2),
            )
        section.add(
            "ops_per_sec_aggregate",
            "Aggregate ops/s",
            round(r.aggregate_ops_per_sec, 2),
        )
        if not sustained_result:
            section.add(
                "ops_per_sec_avg",
                "Avg ops/s",
                round(r.avg_ops_per_sec, 2),
            )
            # Round makespan / keys -- an artifact of the round barrier,
            # not a latency. Kept for output continuity; read the
            # submit_latency_* fields instead.
            section.add(
                "latency_per_key_ms",
                "Avg latency / key (ms)",
                round(r.avg_latency_per_key_ms, 3),
            )
        if r.expected_max_hit_rate > 0 or r.expected_hit_count > 0:
            section.add(
                "expected_max_hit_rate",
                "Expected max hit rate",
                round(r.expected_max_hit_rate, 4),
            )
            section.add(
                "expected_hit_count",
                "Expected hit keys",
                r.expected_hit_count,
            )
            section.add(
                "actual_hit_rate",
                "Actual hit rate",
                round(r.actual_hit_rate, 4),
            )

    read_write_ratio = getattr(args, "read_write_ratio", None)
    if read_write_ratio is not None:
        load_result = next((r for r in results if r.operation == "Load"), None)
        store_result = next((r for r in results if r.operation == "Store"), None)
        if load_result is not None and store_result is not None:
            mixed_section = metrics.add_section("mixed", "Mixed Payloads")
            read_bytes = load_result.total_success_bytes
            write_bytes = store_result.total_success_bytes
            mixed_section.add(
                "read_success_bytes",
                "Successful read payload (bytes)",
                read_bytes,
            )
            mixed_section.add(
                "write_success_bytes",
                "Successful write payload (bytes)",
                write_bytes,
            )
            mixed_section.add(
                "aggregate_success_bytes",
                "Successful aggregate payload (bytes)",
                read_bytes + write_bytes,
            )
            mixed_section.add(
                "read_write_ratio_achieved",
                "Achieved read:write",
                round(read_bytes / write_bytes, 4) if write_bytes else "n/a",
            )

    metrics.emit()
