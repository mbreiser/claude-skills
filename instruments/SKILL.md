---
name: instruments
description: Control Digilent Analog Discovery 3 (AD3) and Saleae Logic Pro 8 test instruments via Python. Use when capturing waveforms, measuring analog signals, generating trigger/stimulus signals, or analyzing oscilloscope/logic analyzer data.
argument-hint: [ad3|saleae] [command]
---

# Test Instrument Control: AD3 & Saleae Logic Pro 8

## Quick Reference

| Instrument | Interface | Python | Key Capability |
|------------|-----------|--------|----------------|
| Digilent AD3 | USB 2.0 (USB-C connector) | `/opt/homebrew/bin/python3.14` (REQUIRED on macOS) | 2-ch scope + 2-ch wavegen + 16-ch digital |
| Saleae Logic Pro 8 | USB 3.0 | System Python OK | 8-ch analog + 8-ch digital logic analyzer |

---

# Part 1: Digilent Analog Discovery 3 (AD3)

## 1.1 Environment Setup

**macOS Sequoia CRITICAL**: System Python 3.9 crashes loading the DWF framework due to a dyld circular rpath bug. You MUST use homebrew Python 3.14:

```bash
# Install (one-time)
brew install python@3.14
/opt/homebrew/bin/python3.14 -m pip install numpy pyserial

# Verify
/opt/homebrew/bin/python3.14 -c "import ctypes; dwf = ctypes.cdll.LoadLibrary('/Library/Frameworks/dwf.framework/dwf'); v = ctypes.create_string_buffer(32); dwf.FDwfGetVersion(v); print(f'DWF SDK v{v.value.decode()}')"
```

**Do NOT use `pydwf`** — it expects SDK v3.20.1 but the installed version is v3.25.1. Use raw ctypes instead.

**DWF SDK location**: `/Library/Frameworks/dwf.framework/dwf`

**Exclusive access**: The WaveForms desktop app and SDK cannot use the device simultaneously. If the device appears busy, kill stale processes: `pkill -9 -f python3.14`

## 1.2 Connection and Device Opening

```python
import ctypes
DWF_LIB_PATH = "/Library/Frameworks/dwf.framework/dwf"
dwf = ctypes.cdll.LoadLibrary(DWF_LIB_PATH)

# Constants
enumfilterAll = ctypes.c_int(0)
acqmodeSingle = ctypes.c_int(0)
acqmodeRecord = ctypes.c_int(3)

# Enumerate and open
hdwf = ctypes.c_int()
c_count = ctypes.c_int()
dwf.FDwfEnum(enumfilterAll, ctypes.byref(c_count))
print(f"Devices found: {c_count.value}")

# Open with Config 1 (32K scope buffer — RECOMMENDED)
# Config 0 = 16K (default), Config 1 = 32K, Config 2 = 8K
dwf.FDwfDeviceConfigOpen(ctypes.c_int(0), ctypes.c_int(1), ctypes.byref(hdwf))
```

**Device configurations** (query with `FDwfEnumConfigInfo`):

| Config | Scope Buffer/ch | Best For |
|--------|----------------|----------|
| 0 | 16,384 samples | General use |
| **1** | **32,768 samples** | **Recommended — max scope buffer** |
| 2 | 8,192 samples | More digital/wavegen resources |

## 1.3 Wavegen (Analog Output)

The AD3 has 2 wavegen channels (W1, W2). Common use: generate stimulus or trigger signals.

```python
# Generate 3.3V square wave at 8 kHz on W1 (e.g., trigger signal)
funcSquare = ctypes.c_byte(2)
ch = ctypes.c_int(0)  # W1
dwf.FDwfAnalogOutNodeEnableSet(hdwf, ch, ctypes.c_int(0), ctypes.c_int(1))
dwf.FDwfAnalogOutNodeFunctionSet(hdwf, ch, ctypes.c_int(0), funcSquare)
dwf.FDwfAnalogOutNodeFrequencySet(hdwf, ch, ctypes.c_int(0), ctypes.c_double(8000.0))
dwf.FDwfAnalogOutNodeAmplitudeSet(hdwf, ch, ctypes.c_int(0), ctypes.c_double(3.3))  # Vpp
dwf.FDwfAnalogOutNodeOffsetSet(hdwf, ch, ctypes.c_int(0), ctypes.c_double(1.65))    # center
dwf.FDwfAnalogOutNodeSymmetrySet(hdwf, ch, ctypes.c_int(0), ctypes.c_double(50.0))  # duty %
dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(1))  # start

# Stop
dwf.FDwfAnalogOutConfigure(hdwf, ch, ctypes.c_int(0))
```

Available waveform functions: DC(0), Sine(1), Square(2), Triangle(3), RampUp(4), RampDown(5), Noise(6), Pulse(7), Custom(30).

## 1.4 Scope — Triggered Single-Shot Mode (PREFERRED)

Captures into on-device FPGA buffer at up to **100 MHz**. No USB streaming bottleneck. Transfer happens after acquisition completes. This is the preferred mode for any signal shorter than the buffer window.

**Buffer window = buffer_size / sample_rate**:

| Rate | 16K buffer | 32K buffer (Config 1) |
|------|-----------|----------------------|
| 100 MHz | 164 us | **328 us** |
| 50 MHz | 328 us | 655 us |
| 12.5 MHz | 1,311 us | **2,621 us** |
| 10 MHz | 1,638 us | 3,277 us |
| 1 MHz | 16.4 ms | 32.8 ms |

```python
import numpy as np

# Configure channels
dwf.FDwfAnalogInChannelEnableSet(hdwf, ctypes.c_int(0), ctypes.c_int(1))  # Ch1 ON
dwf.FDwfAnalogInChannelEnableSet(hdwf, ctypes.c_int(1), ctypes.c_int(1))  # Ch2 ON
dwf.FDwfAnalogInChannelRangeSet(hdwf, ctypes.c_int(0), ctypes.c_double(0.5))   # Ch1: 500mV
dwf.FDwfAnalogInChannelRangeSet(hdwf, ctypes.c_int(1), ctypes.c_double(5.0))   # Ch2: 5V

# Single-shot mode, 12.5 MHz, 32K buffer
dwf.FDwfAnalogInAcquisitionModeSet(hdwf, acqmodeSingle)
dwf.FDwfAnalogInFrequencySet(hdwf, ctypes.c_double(12.5e6))
dwf.FDwfAnalogInBufferSizeSet(hdwf, ctypes.c_int(32768))

# Trigger on W1 output (hardware-aligned, zero jitter)
trigsrcAnalogOut1 = ctypes.c_byte(7)
dwf.FDwfAnalogInTriggerSourceSet(hdwf, trigsrcAnalogOut1)
dwf.FDwfAnalogInTriggerAutoTimeoutSet(hdwf, ctypes.c_double(5.0))

# Pre-trigger: 5% of buffer before trigger edge
buf_size = 32768
sr = 12.5e6
dwf.FDwfAnalogInTriggerPositionSet(hdwf, ctypes.c_double(buf_size * 0.95 / sr))

# Capture loop
DwfStateDone = ctypes.c_byte(2)
N_CAPTURES = 100
captures_ch1 = np.zeros((N_CAPTURES, buf_size), dtype=np.float64)
captures_ch2 = np.zeros((N_CAPTURES, buf_size), dtype=np.float64)
buf1 = (ctypes.c_double * buf_size)()
buf2 = (ctypes.c_double * buf_size)()

for i in range(N_CAPTURES):
    dwf.FDwfAnalogInConfigure(hdwf, ctypes.c_int(0), ctypes.c_int(1))  # arm
    sts = ctypes.c_byte()
    while True:
        dwf.FDwfAnalogInStatus(hdwf, ctypes.c_int(1), ctypes.byref(sts))
        if sts.value == DwfStateDone.value:
            break
        time.sleep(0.00005)
    dwf.FDwfAnalogInStatusData(hdwf, ctypes.c_int(0), buf1, ctypes.c_int(buf_size))
    dwf.FDwfAnalogInStatusData(hdwf, ctypes.c_int(1), buf2, ctypes.c_int(buf_size))
    captures_ch1[i] = np.frombuffer(buf1, dtype=np.float64)
    captures_ch2[i] = np.frombuffer(buf2, dtype=np.float64)

# Time axis (t=0 at trigger)
pre_samples = int(buf_size * 0.05)
t_us = (np.arange(buf_size) - pre_samples) / sr * 1e6
```

**Trigger source options**:
- `trigsrcAnalogOut1 = c_byte(7)` — trigger from W1 (best for self-generated triggers)
- `trigsrcDetectorAnalogIn = c_byte(2)` — trigger on scope channel threshold crossing
- `trigsrcExternal1 = c_byte(11)` — external trigger input
- `trigsrcNone = c_byte(0)` — immediate (no trigger)

For detector trigger, also set channel, level, and edge:
```python
dwf.FDwfAnalogInTriggerSourceSet(hdwf, ctypes.c_byte(2))   # detector
dwf.FDwfAnalogInTriggerChannelSet(hdwf, ctypes.c_int(1))    # Ch2
dwf.FDwfAnalogInTriggerLevelSet(hdwf, ctypes.c_double(1.5)) # 1.5V threshold
dwf.FDwfAnalogInTriggerConditionSet(hdwf, ctypes.c_int(0))  # 0=rising, 1=falling
```

**Re-arm rate**: ~137 captures/sec dual-channel, ~727/sec single-channel.

## 1.5 Scope — Record (Streaming) Mode

For long captures that exceed the FPGA buffer. Data streams over USB 2.0 in real-time. **USB bandwidth is the bottleneck.**

| Channels | Max Clean Rate | Total USB BW |
|----------|---------------|-------------|
| 1 ch | **5 MHz** | 5 MS/s |
| 2 ch | **2 MHz** | 4 MS/s |

Above these rates: `corrupted` counter in `FDwfAnalogInStatusRecord` will be nonzero.

```python
dwf.FDwfAnalogInAcquisitionModeSet(hdwf, acqmodeRecord)
dwf.FDwfAnalogInFrequencySet(hdwf, ctypes.c_double(4e6))       # 4 MHz (single ch)
dwf.FDwfAnalogInRecordLengthSet(hdwf, ctypes.c_double(2.0))    # 2 seconds
dwf.FDwfAnalogInBufferSizeSet(hdwf, ctypes.c_int(32768))       # max FPGA FIFO

# Disable unused channel to maximize bandwidth
dwf.FDwfAnalogInChannelEnableSet(hdwf, ctypes.c_int(1), ctypes.c_int(0))

# Start
dwf.FDwfAnalogInConfigure(hdwf, ctypes.c_int(0), ctypes.c_int(1))

# Streaming collection loop — pre-allocate, minimize work in hot loop
n_samples = int(2.0 * 4e6)
result = np.empty(n_samples, dtype=np.float64)
c_available = ctypes.c_int()
c_lost = ctypes.c_int()
c_corrupted = ctypes.c_int()
buf = (ctypes.c_double * 65536)()
collected = 0

while collected < n_samples:
    sts = ctypes.c_byte()
    dwf.FDwfAnalogInStatus(hdwf, ctypes.c_int(1), ctypes.byref(sts))
    dwf.FDwfAnalogInStatusRecord(hdwf, ctypes.byref(c_available),
                                  ctypes.byref(c_lost), ctypes.byref(c_corrupted))
    if c_available.value > 0:
        to_read = min(c_available.value, 65536, n_samples - collected)
        dwf.FDwfAnalogInStatusData(hdwf, ctypes.c_int(0), buf, ctypes.c_int(to_read))
        result[collected:collected+to_read] = np.frombuffer(buf, dtype=np.float64, count=to_read)
        collected += to_read
    if sts.value == 2:  # DwfStateDone
        break
    time.sleep(0.001)
```

**Optimization tips** (from `ad3_record_mode_optimization.md`):
- Pre-allocate numpy arrays (no list append in hot loop)
- Set max FPGA FIFO: `FDwfAnalogInBufferSizeSet(hdwf, c_int(32768))`
- Disable unused channels explicitly
- Throttle progress reporting (avoid I/O in hot loop)
- Use root USB port (not through a hub)

## 1.6 Cleanup

```python
dwf.FDwfAnalogOutConfigure(hdwf, ctypes.c_int(0), ctypes.c_int(0))  # stop W1
dwf.FDwfAnalogOutConfigure(hdwf, ctypes.c_int(1), ctypes.c_int(0))  # stop W2
dwf.FDwfDeviceClose(hdwf)
```

## 1.7 Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| `dyld` crash / segfault on import | macOS Sequoia + system Python 3.9 | Use `/opt/homebrew/bin/python3.14` |
| "JTAG init failed" / device busy | Stale process holding device | `pkill -9 -f python3.14`, maybe USB replug |
| 4+ MHz dual-ch corrupted in record mode | USB 2.0 bandwidth wall | Use single-ch, or switch to triggered mode |
| `pydwf` version mismatch error | pydwf expects SDK 3.20.1 | Don't use pydwf; use raw ctypes |
| Buffer too small for capture window | Default Config 0 = 16K | Open with Config 1 for 32K |
| WaveForms app won't connect | SDK has exclusive lock | Close Python, or vice versa |

---

# Part 2: Saleae Logic Pro 8

## 2.1 Environment Setup

The Saleae Logic Pro 8 uses the **Logic 2 automation API** (gRPC-based). The Logic 2 desktop app must be running with automation enabled.

```bash
# Install the Python API
pip install logic2-automation

# In Logic 2 app: Preferences → Enable automation server (default port 10430)
```

## 2.2 Connection

```python
from saleae import automation

# Connect to running Logic 2 instance
manager = automation.Manager.connect(port=10430)
```

## 2.3 Capture Configuration

```python
# Digital + analog capture
device_config = automation.LogicDeviceConfiguration(
    enabled_digital_channels=[0, 1, 2, 3],     # digital channels
    enabled_analog_channels=[0, 1],              # analog channels
    digital_sample_rate=50_000_000,              # 50 MHz digital
    analog_sample_rate=6_250_000,                # 6.25 MHz analog (max for 2+ ch)
    digital_threshold_volts=1.5,                 # logic threshold
)
```

**Analog sample rates** (Logic Pro 8):
- 1 channel: up to 50 MHz
- 2 channels: up to 6.25 MHz
- 4 channels: up to 3.125 MHz
- 8 channels: up to 1.5625 MHz

## 2.4 Triggered Capture

```python
# Timer-based capture
capture_config = automation.CaptureConfiguration(
    capture_mode=automation.TimerCaptureMode(duration_seconds=2.0)
)

# Or trigger-based: start capture, wait for trigger
capture_config = automation.CaptureConfiguration(
    capture_mode=automation.DigitalTriggerCaptureMode(
        trigger_type=automation.DigitalTriggerType.RISING,
        trigger_channel_index=0,
        after_trigger_seconds=1.0,
    )
)

# Start capture
capture = manager.start_capture(
    device_configuration=device_config,
    capture_configuration=capture_config,
)
capture.wait()  # blocks until complete
```

## 2.5 Export and Analysis

```python
# Export analog data to CSV
analog_export_config = automation.ExportAnalogDataConfiguration(
    channels=[0, 1],
    output_file_path="/tmp/saleae_analog.csv",
    analog_downsample_ratio=1,
)
capture.export_analog_data(analog_export_config)

# Export raw binary (faster for large captures)
capture.export_raw_data_binary(
    directory="/tmp/saleae_raw/",
    analog_channels=[0, 1],
    digital_channels=[0],
)

# Or load directly via numpy after CSV export
import numpy as np
data = np.loadtxt("/tmp/saleae_analog.csv", delimiter=",", skiprows=1)
```

## 2.6 Protocol Analyzers

```python
# Add SPI analyzer
spi = capture.add_analyzer("SPI", label="SPI Bus", settings={
    "MISO": 0,
    "Clock": 1,
    "Enable": 2,
    "Bits per Transfer": "8 Bits per Transfer",
})

# Export analyzer results
capture.export_data_table(
    filepath="/tmp/spi_results.csv",
    analyzers=[spi],
)
```

## 2.7 Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| Connection refused on port 10430 | Logic 2 automation not enabled | Preferences → Enable automation |
| Low analog sample rate | Too many channels enabled | Reduce active analog channels |
| Capture hangs | Trigger never fires | Use timer mode, or check trigger settings |
| "Device not found" | USB issue | Reconnect USB, restart Logic 2 |

---

# Part 3: G6 LED Panel Test Integration

This section covers patterns specific to the G6 20x20 LED panel project. See the project's `CLAUDE.md` for full context.

## 3.1 AD3 as Trigger Generator + Scope (replaces external function generator + Saleae)

Wiring:
- **W1** (wavegen output) → **GP45** (MCU external trigger input, bodge wire)
- **Ch1+** (scope) → Photodiode output
- **Ch1-** → GND
- **Ch2+** (scope) → GP45 (tee with W1, trigger reference)
- **Ch2-** → GND

## 3.2 BCMBURST Capture Workflow

```python
# 1. Configure MCU via serial
send_cmd(ser, "EXTTRIG ON")
send_cmd(ser, "BCM 4")
send_cmd(ser, "BCMON 0.5")
send_cmd(ser, "ROWS 20")
send_cmd(ser, "FILL 0")
for c in range(20):
    send_cmd(ser, f"PIXEL 10 {c} 15")  # row 10 all cols

# 2. Start wavegen (BEFORE BCMBURST — avoids trigger timeout race)
ad3.wavegen_trigger(channel=0, freq_hz=8000.0)
time.sleep(0.3)

# 3. Start BCMBURST
ser.write(b"BCMBURST 100000\r\n")
time.sleep(0.5)

# 4. Capture (triggered mode, 12.5 MHz dual-channel)
result = ad3.scope_triggered(n_captures=500, sample_rate=12_500_000,
                              channels=(0, 1), trigger_source="analogout1")

# 5. Stop
ad3.wavegen_stop(0)
send_cmd(ser, "EXTTRIG OFF")
```

## 3.3 Row Classification for BCMBURST

BCMBURST scans one row per trigger, cycling 0→19→0. Each triggered capture hits a random row position. To isolate a specific row's signal:

```python
# Find trigger edges in Ch2 average
ch2_avg = result["ch2_avg"]
thresh = (ch2_avg.min() + ch2_avg.max()) / 2
ref_edges = np.where(np.diff((ch2_avg > thresh).astype(np.int8)) == 1)[0]

# For each capture, find which trigger edge has peak PD signal
for i in range(N):
    for j, edge in enumerate(ref_edges):
        peak = (ch1[i, edge:edge+window] - baseline).max()
        # Assign capture to edge with highest peak

# Average only same-edge captures for row-specific signal
```

## 3.4 Key Script

`test_firmware/single_led/ad3_capture.py` — full AD3 integration with AD3 class, serial helpers, record mode, triggered mode, bcmburst capture, photocal capture, and CLI interface. Shebang is `/opt/homebrew/bin/python3.14`.

## 3.5 Measured Parameters

- **Trigger-to-LED onset**: 1.5-1.6 us (GP45 rising edge to photodiode response)
- **AD3 wavegen trigger jitter**: ~1.4 us (acceptable for optical characterization, not for jitter measurement — use DWT for jitter)
- **pixel_data[10] maps to 2 physical rows** (trigger offsets 3 and 12 in 20-row cycle) due to NUM_COLOR=4 coordinate interleaving — needs investigation
