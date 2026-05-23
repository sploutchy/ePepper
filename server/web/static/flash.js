// Custom flasher for /app/flash. Replaces ESP Web Tools (which requires
// Web Serial, desktop-only) with a Web USB path that works on Android
// Chrome too. esptool-js takes a SerialPort-shaped object; we polyfill
// one over a USBDevice (CDC ACM class — the ESP32-S3 native USB exposes
// exactly that in download mode).

import { ESPLoader, Transport } from "https://unpkg.com/esptool-js@0.6.0/bundle.js";

// --- Web USB → SerialPort polyfill (CDC ACM only) -------------------------

class WebUsbSerialPort {
  constructor(usbDevice) {
    this.device = usbDevice;
    this.commInterface = null;
    this.dataInterface = null;
    this.endpointIn = null;
    this.endpointOut = null;
    // Web Serial's setSignals only updates the named signals; track state so
    // a partial call doesn't clobber the other signal.
    this._dtr = false;
    this._rts = false;
    this._readable = null;
    this._writable = null;
  }

  getInfo() {
    return {
      usbVendorId: this.device.vendorId,
      usbProductId: this.device.productId,
    };
  }

  async open({ baudRate }) {
    if (!this.device.opened) {
      await this.device.open();
    }
    if (this.device.configuration === null) {
      await this.device.selectConfiguration(1);
    }

    for (const iface of this.device.configuration.interfaces) {
      const alt = iface.alternate;
      if (alt.interfaceClass === 0x02) {
        // CDC Communication Interface — carries SET_LINE_CODING /
        // SET_CONTROL_LINE_STATE control transfers.
        this.commInterface = iface.interfaceNumber;
      } else if (alt.interfaceClass === 0x0A) {
        // CDC Data Interface — bulk IN/OUT.
        this.dataInterface = iface.interfaceNumber;
        for (const ep of alt.endpoints) {
          if (ep.type !== "bulk") continue;
          if (ep.direction === "in") this.endpointIn = ep.endpointNumber;
          if (ep.direction === "out") this.endpointOut = ep.endpointNumber;
        }
      }
    }

    if (this.dataInterface === null || this.endpointIn === null || this.endpointOut === null) {
      throw new Error("No CDC data interface / bulk endpoints found on this USB device.");
    }

    await this.device.claimInterface(this.dataInterface);
    if (this.commInterface !== null) {
      // Best-effort — on some hosts the comm interface is auto-claimed by
      // the kernel and we just rely on the control transfers going through
      // without an explicit claim.
      try { await this.device.claimInterface(this.commInterface); } catch (_) {}
    }

    await this._setLineCoding(baudRate);
    this._setupStreams();
  }

  async _setLineCoding(baudRate) {
    if (this.commInterface === null) return;
    const buffer = new ArrayBuffer(7);
    const view = new DataView(buffer);
    view.setUint32(0, baudRate, true); // dwDTERate, little-endian
    view.setUint8(4, 0); // 1 stop bit
    view.setUint8(5, 0); // no parity
    view.setUint8(6, 8); // 8 data bits
    await this.device.controlTransferOut({
      requestType: "class",
      recipient: "interface",
      request: 0x20, // SET_LINE_CODING
      value: 0,
      index: this.commInterface,
    }, buffer);
  }

  _setupStreams() {
    const dev = this.device;
    const epIn = this.endpointIn;
    const epOut = this.endpointOut;

    this._readable = new ReadableStream({
      async pull(controller) {
        try {
          const result = await dev.transferIn(epIn, 64);
          if (result.status === "stall") {
            await dev.clearHalt("in", epIn);
            return;
          }
          if (result.data && result.data.byteLength > 0) {
            controller.enqueue(new Uint8Array(result.data.buffer));
          }
        } catch (e) {
          controller.error(e);
        }
      },
    });

    this._writable = new WritableStream({
      async write(chunk) {
        const result = await dev.transferOut(epOut, chunk);
        if (result.status === "stall") {
          await dev.clearHalt("out", epOut);
        }
      },
    });
  }

  get readable() { return this._readable; }
  get writable() { return this._writable; }

  async setSignals({ dataTerminalReady, requestToSend } = {}) {
    if (this.commInterface === null) return;
    if (dataTerminalReady !== undefined) this._dtr = !!dataTerminalReady;
    if (requestToSend !== undefined) this._rts = !!requestToSend;
    let value = 0;
    if (this._dtr) value |= 0x01;
    if (this._rts) value |= 0x02;
    await this.device.controlTransferOut({
      requestType: "class",
      recipient: "interface",
      request: 0x22, // SET_CONTROL_LINE_STATE
      value,
      index: this.commInterface,
    });
  }

  async close() {
    try {
      if (this.dataInterface !== null) await this.device.releaseInterface(this.dataInterface);
    } catch (_) {}
    try {
      if (this.commInterface !== null) await this.device.releaseInterface(this.commInterface);
    } catch (_) {}
  }
}

// --- Helpers --------------------------------------------------------------

// esptool-js's writeFlash takes the image as a binary string, not Uint8Array.
// Chunk the conversion — passing a 1.5 MB array to fromCharCode.apply in one
// call blows the argument-list stack limit on most engines.
function uint8ToBinaryString(u8) {
  const CHUNK = 0x8000;
  let s = "";
  for (let i = 0; i < u8.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK));
  }
  return s;
}

// --- UI -------------------------------------------------------------------

const startBtn = document.getElementById("flash-start");
const logEl = document.getElementById("flash-log");
const progressEl = document.getElementById("flash-progress");
const progressFill = document.getElementById("flash-progress-fill");

function log(line) {
  if (!logEl) return;
  logEl.textContent += line + "\n";
  logEl.scrollTop = logEl.scrollHeight;
}

function showProgress(written, total) {
  if (!progressEl) return;
  progressEl.hidden = false;
  const pct = total > 0 ? (written / total) * 100 : 0;
  progressFill.style.width = pct.toFixed(1) + "%";
}

if (!("usb" in navigator)) {
  startBtn.disabled = true;
  startBtn.textContent = "Web USB unavailable";
  log("Web USB isn't available in this browser. Use Chrome or Edge (desktop, or Android over OTG).");
} else {
  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    logEl.textContent = "";
    try {
      log("Select the ePepper device in the picker (it must be in download mode — hold BOOT, tap RESET, then release BOOT).");
      // Espressif's USB vendor ID. The S3 ROM bootloader uses 0x1001;
      // some app firmwares use other PIDs under the same vendor, so we
      // filter on vendor only and let the user pick.
      const usbDevice = await navigator.usb.requestDevice({
        filters: [{ vendorId: 0x303A }],
      });

      const port = new WebUsbSerialPort(usbDevice);
      const transport = new Transport(port, false);

      const loader = new ESPLoader({
        transport,
        baudrate: 115200,
        romBaudrate: 115200,
        terminal: {
          clean() { logEl.textContent = ""; },
          writeLine(s) { log(s); },
          write(s) { log(s.replace(/\n$/, "")); },
        },
      });

      log("Connecting…");
      const chip = await loader.main();
      log(`Detected: ${chip}`);

      log("Fetching firmware…");
      const resp = await fetch("/app/flash/epepper-merged.bin", { cache: "no-cache" });
      if (!resp.ok) throw new Error(`Could not fetch firmware: HTTP ${resp.status}`);
      const buf = await resp.arrayBuffer();
      const data = uint8ToBinaryString(new Uint8Array(buf));
      log(`Firmware size: ${buf.byteLength.toLocaleString()} bytes`);

      log("Erasing flash…");
      await loader.eraseFlash();

      log("Writing flash (this takes ~1 min)…");
      await loader.writeFlash({
        fileArray: [{ data, address: 0 }],
        flashSize: "keep",
        flashMode: "keep",
        flashFreq: "keep",
        eraseAll: false,
        compress: true,
        reportProgress: (idx, written, total) => showProgress(written, total),
      });

      log("Resetting…");
      await loader.hardReset();
      log("Done. The device should boot the new firmware now.");
    } catch (err) {
      log("Error: " + (err && err.message ? err.message : String(err)));
      console.error(err);
    } finally {
      startBtn.disabled = false;
    }
  });
}
