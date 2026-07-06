# Scenario Diagrams

Sequence diagrams for each benchmark scenario showing data flow between
initiator (NVIDIA RTX 5090 host), network (400G), and target (Xeon + IPU + SSD).

## Scenario 1: L1 Hit — RDMA Serve (Hot Path)

```mermaid
sequenceDiagram
    participant I as Initiator<br/>(vLLM, RTX 5090)
    participant N as 400G Network
    participant IPU as Target IPU<br/>(Falcon DMA)
    participant DRAM as Target DRAM<br/>(LMCache)

    Note over I: Tier 1 miss → need KV page
    I->>N: RDMA Read Request (64B)
    N->>IPU: RDMA Read Request
    IPU->>DRAM: DMA Read (256KB page)
    DRAM-->>IPU: Page data
    IPU->>N: DMA gather (header + 256KB payload)
    N->>I: RDMA Read Response (256KB)

    Note over IPU,DRAM: CPU never touches data bytes<br/>IPU streams DRAM → wire via DMA
```

## Scenario 2: L1 Hit — NVMe/TCP Serve (Hot Path)

```mermaid
sequenceDiagram
    participant I as Initiator<br/>(vLLM, RTX 5090)
    participant N as 400G Network
    participant IPU as Target IPU<br/>(TSO offload)
    participant DRAM as Target DRAM<br/>(LMCache)

    Note over I: Tier 1 miss → NVMe Read command
    I->>N: NVMe Read Cmd Capsule (72B)
    N->>IPU: Command capsule
    IPU->>DRAM: DMA Read page (256KB)
    DRAM-->>IPU: Page data

    Note over IPU: TSO segments 256KB into 4x 64KB frames
    IPU->>N: C2HData PDU [segment 1/4] (64KB)
    IPU->>N: C2HData PDU [segment 2/4] (64KB)
    IPU->>N: C2HData PDU [segment 3/4] (64KB)
    IPU->>N: C2HData PDU [segment 4/4] (64KB)
    IPU->>N: NVMe CQE (16B)
    N->>I: Segments reassembled (256KB)

    Note over IPU: TSO = hardware segmentation<br/>CPU never touches data<br/>Validate: cpu_util < 10%
```

## Scenario 3: L1 Miss — SSD Fetch → RDMA Serve (Cold Path)

```mermaid
sequenceDiagram
    participant I as Initiator<br/>(vLLM, RTX 5090)
    participant N as 400G Network
    participant CPU as Target CPU<br/>(LMCache)
    participant DRAM as Target DRAM
    participant SSD as NVMe SSDs<br/>(8x per host)
    participant IPU as Target IPU

    Note over I: Tier 1 miss - request KV page
    I->>N: RDMA Read Request (64B)
    N->>CPU: RDMA Read arrives
    CPU->>DRAM: Lookup page address

    Note over DRAM: MISS - page not in DRAM
    CPU->>SSD: io_uring/SPDK read (256KB)
    Note over SSD: NVMe read ~50-80us
    SSD-->>DRAM: Page data staged in DRAM
    CPU->>DRAM: Index page (now hot)

    Note over CPU: Page ready - hand off to IPU for DMA
    IPU->>DRAM: DMA Read (256KB)
    DRAM-->>IPU: Page data
    IPU->>N: RDMA Read Response (256KB)
    N->>I: Page data (256KB)

    Note over SSD,I: Miss penalty = SSD latency (~80us)<br/>vs hit latency (~5us)
```

## Scenario 4: L1 Miss — SSD Fetch → NVMe/TCP Serve (Cold Path)

```mermaid
sequenceDiagram
    participant I as Initiator<br/>(vLLM, RTX 5090)
    participant N as 400G Network
    participant CPU as Target CPU<br/>(LMCache)
    participant DRAM as Target DRAM
    participant SSD as NVMe SSDs
    participant IPU as Target IPU<br/>(TSO)

    I->>N: NVMe Read Cmd (72B)
    N->>CPU: Command capsule
    CPU->>DRAM: Lookup page

    Note over DRAM: MISS
    CPU->>SSD: io_uring read (256KB)
    Note over SSD: ~50-80us
    SSD-->>DRAM: Page staged
    CPU->>DRAM: Index page (now hot)

    IPU->>DRAM: DMA Read (256KB)
    DRAM-->>IPU: Page data
    Note over IPU: TSO segments into 4x 64KB
    IPU->>N: C2HData PDUs (4x 64KB)
    IPU->>N: NVMe CQE (16B)
    N->>I: Page data (256KB)

    Note over SSD,I: Total = SSD latency + NVMe/TCP overhead<br/>Compare with Scenario 3 to isolate NVMe/TCP cost
```

## Scenario 5A: Write — Raw RDMA Pull Model

```mermaid
sequenceDiagram
    participant I as Initiator<br/>(new KV computed)
    participant N as 400G Network
    participant IPU as Target IPU
    participant DRAM as Target DRAM<br/>(LMCache)
    participant CPU as Target CPU<br/>(LMCache control)

    Note over I: Inference produced new KV page
    I->>N: Write intent (128B: hash + size + src_addr)
    N->>CPU: Control message

    Note over CPU: LMCache decision loop:<br/>1. Pool has free slot? → allocate<br/>2. Pool full? → evict cold page first
    CPU->>DRAM: Allocate 256KB buffer at addr Y
    Note over CPU: Post RDMA Read: "pull from initiator addr X → my addr Y"
    CPU->>IPU: RDMA Read descriptor (src=X, dst=Y, len=256KB)

    IPU->>N: RDMA Read Request (64B) to initiator
    N->>I: RDMA Read (read initiator memory at X)
    I-->>N: RDMA Read Response (256KB)
    N->>IPU: Data arrives
    IPU->>DRAM: DMA Write to addr Y (256KB)

    Note over CPU: Data landed → index page
    CPU->>DRAM: Update hash table (chunk_hash → addr Y)

    Note over CPU,IPU: Key: target chose WHEN to pull (flow control)<br/>and WHERE to store (buffer management)<br/>Initiator had no say in timing
```

## Scenario 5B: Write — NVMe-oF Pull Model

```mermaid
sequenceDiagram
    participant I as Initiator
    participant N as 400G Network
    participant IPU as Target IPU
    participant DRAM as Target DRAM<br/>(LMCache)
    participant SPDK as Target SPDK<br/>(NVMe-oF target)

    Note over I: NVMe Write command (data NOT inline)
    I->>N: NVMe Write Cmd Capsule (72B, control only)
    N->>SPDK: Command: "write 256KB"

    Note over SPDK: SPDK decides when and where:
    SPDK->>DRAM: LMCache allocate buffer at addr Y
    Note over SPDK: Post RDMA Read to pull data
    SPDK->>IPU: RDMA Read (src=initiator:X, dst=Y, len=256KB)

    IPU->>N: RDMA Read to initiator (64B)
    I-->>N: RDMA Read Response (256KB)
    N->>IPU: Data
    IPU->>DRAM: DMA Write to addr Y

    SPDK->>DRAM: Index page (LMCache)
    SPDK->>N: NVMe CQE (16B, completion)
    N->>I: Write complete

    Note over SPDK,IPU: Same pull semantics as 5A<br/>Extra overhead: NVMe cmd parse + CQE = ~88B + processing<br/>Benefit: standard NVMe-oF storage interface
```

## Scenario 6: Write — NVMe/TCP Pull (R2T)

```mermaid
sequenceDiagram
    participant I as Initiator
    participant N as 400G Network (TCP)
    participant IPU as Target IPU<br/>(TSO/offload)
    participant DRAM as Target DRAM<br/>(LMCache)

    I->>N: NVMe Write Cmd Capsule (72B, no inline data)
    N->>IPU: TCP → command

    Note over DRAM: LMCache: evict/allocate buffer at addr Y
    IPU->>N: R2T - Ready to Transfer (16B)
    Note over IPU: "Send me 256KB now"
    N->>I: R2T (tells initiator: you may send data)

    Note over I: Initiator sends data ONLY after R2T
    I->>N: H2CData PDU (256KB, segmented by initiator TSO)
    N->>IPU: H2CData
    IPU->>DRAM: DMA Write to addr Y (256KB)

    Note over DRAM: LMCache indexes page
    IPU->>N: NVMe CQE (16B)
    N->>I: Write complete

    Note over IPU,I: R2T = TCP equivalent of RDMA Read<br/>Target controls timing via R2T<br/>Extra half-RTT latency vs raw RDMA (~2.5us)
```

## Scenario 7: Mixed Read/Write (5:1 Steady State)

```mermaid
sequenceDiagram
    participant I1 as Initiator 1<br/>(reading)
    participant I2 as Initiator 2<br/>(writing)
    participant N as 400G Network
    participant IPU as Target IPU
    participant DRAM as Target DRAM<br/>(LMCache)

    Note over N: Concurrent reads (5 streams) + writes (1 stream)

    par Read streams (TX dominant)
        I1->>N: RDMA Read Req (64B)
        N->>IPU: Read req
        IPU->>DRAM: DMA Read
        DRAM-->>IPU: 256KB
        IPU->>N: 256KB (TX)
        N->>I1: Page data
    and Write stream (RX)
        I2->>N: Write intent (128B)
        N->>IPU: Control msg
        Note over DRAM: Evict + alloc
        IPU->>N: RDMA Read to I2 (64B)
        N->>I2: Pull request
        I2-->>N: 256KB (RX at target)
        N->>IPU: Data
        IPU->>DRAM: DMA Write 256KB
    end

    Note over N: TX:RX ratio ~= 5:1<br/>TX = read responses (5 x 256KB)<br/>RX = write pull (1 x 256KB)<br/>Validate: reads don't degrade under writes
```

## Scenario 8: Eviction Pipeline (Full DRAM)

```mermaid
sequenceDiagram
    participant I as Initiator<br/>(write intent)
    participant CPU as Target CPU<br/>(LMCache)
    participant DRAM as Target DRAM<br/>(FULL)
    participant SSD as NVMe SSD<br/>(cold tier)
    participant IPU as Target IPU

    I->>CPU: Write intent (128B)

    Note over CPU: DRAM is FULL → must evict before allocating

    rect rgb(255, 240, 240)
        Note over CPU,SSD: Eviction pipeline (serial, latency-critical)
        CPU->>DRAM: Select cold page (LRU policy, ~1us)
        CPU->>SSD: Async flush cold page (io_uring, fire-and-forget)
        Note over SSD: SSD write proceeds in background<br/>Slot freed IMMEDIATELY (async)
        CPU->>DRAM: Free slot → return to pool (~1us)
    end

    rect rgb(240, 255, 240)
        Note over CPU,IPU: Allocation + Pull (can now proceed)
        CPU->>DRAM: Allocate free slot at addr Y (~1us)
        CPU->>IPU: Post RDMA Read (src=initiator, dst=Y, 256KB)
        IPU->>I: RDMA Read Request
        I-->>IPU: 256KB data
        IPU->>DRAM: DMA Write to addr Y
    end

    CPU->>DRAM: Index new page (~1us)

    Note over CPU,SSD: Total pipeline: ~12us (async flush)<br/>If sync flush: +80us (UNACCEPTABLE)<br/>Required rate: 40K pages/sec<br/>Async capacity: 83K/sec (PASS)<br/>Sync capacity: 10K/sec (FAIL)
```

## Scenario 9: Multi-Initiator Write Flood

```mermaid
sequenceDiagram
    participant I1 as Initiator 1
    participant I2 as Initiator 2
    participant I3 as Initiator 3
    participant I4 as Initiator 4
    participant Q as Write Intent Queue<br/>(bounded, no data)
    participant CPU as Target CPU<br/>(LMCache)
    participant DRAM as Target DRAM
    participant IPU as Target IPU

    Note over I1,I4: 4 initiators burst write intents simultaneously

    par Burst arrival (control messages only, ~1MB total)
        I1->>Q: 80 intents (128B each = 10KB)
        I2->>Q: 80 intents (10KB)
        I3->>Q: 80 intents (10KB)
        I4->>Q: 80 intents (10KB)
    end

    Note over Q: 320 intents queued<br/>Control msgs only = 40KB<br/>(NOT 80MB of data!)

    loop Target drains at own pace
        CPU->>Q: Dequeue intent
        CPU->>DRAM: Evict + allocate
        CPU->>IPU: Post RDMA Read
        IPU->>I1: Pull 256KB
        I1-->>DRAM: Data arrives
        CPU->>DRAM: Index page
    end

    Note over Q,DRAM: PULL: target drains 320 intents in ~10ms<br/>Queue bounded, no drops, no fabric backpressure

    Note over Q,DRAM: PUSH (rejected): 320 x 256KB = 80MB in less than 1ms<br/>Would need 80MB pre-allocated receive buffers<br/>32KB IPU cache makes this impossible
```

## Architecture Overview (All Scenarios)

```mermaid
graph LR
    subgraph Initiator ["INITIATOR (NVIDIA RTX 5090 Host)"]
        direction TB
        vLLM[vLLM Engine]
        T0[Tier 0: GPU HBM]
        T1[Tier 1: Host DRAM]
        REG_I[Registered Memory]
        vLLM --> T0
        T0 -->|miss| T1
    end

    subgraph Target ["TARGET (Xeon Storage Server)"]
        direction TB
        subgraph IPU_Block ["IPU (Falcon Offload)"]
            direction LR
            DMA_R[DMA Read]
            DMA_W[DMA Write]
            TSO[TSO/GSO]
        end

        subgraph LMC ["LMCache (Memory Mgmt Plane)"]
            direction TB
            CTRL[Admission Control]
            EVICT[Eviction Policy]
            ALLOC[Buffer Allocator]
            INDEX[Page Index]
        end

        T2[Tier 2: Registered DRAM 64 GiB]
        T3[Tier 3: 8x NVMe SSDs]
    end

    T1 -->|"READ: miss at Tier 1"| DMA_R
    DMA_R -->|DMA from| T2
    DMA_R -->|TX response| TSO

    T1 -->|"WRITE: intent"| CTRL
    CTRL --> EVICT
    EVICT -->|free slot| ALLOC
    ALLOC -->|"post RDMA Read"| DMA_W
    DMA_W -->|"pull from initiator"| REG_I
    DMA_W -->|store| T2
    T2 --> INDEX

    EVICT -.->|async flush| T3
    T3 -.->|miss fetch| T2

    style IPU_Block fill:#e1f5fe
    style LMC fill:#f3e5f5
    style T2 fill:#e8f5e9
    style T3 fill:#fff3e0
```

## Read vs Write Data Flow Summary

```mermaid
graph LR
    subgraph READ ["READ PATH (TX dominant)"]
        direction LR
        R1[RDMA Read Req<br/>64B RX] --> R2[DMA from DRAM<br/>256KB]
        R2 --> R3[DMA gather<br/>header + payload]
        R3 --> R4[TX to wire<br/>256KB]
    end

    subgraph WRITE ["WRITE PATH (RX, pull model)"]
        direction LR
        W1[Write intent<br/>128B RX] --> W2[LMCache:<br/>evict + alloc]
        W2 --> W3[Post RDMA Read<br/>64B TX]
        W3 --> W4[Data arrives<br/>256KB RX]
        W4 --> W5[DMA to DRAM<br/>+ index]
    end

    style READ fill:#e8f5e9
    style WRITE fill:#fff3e0
```
