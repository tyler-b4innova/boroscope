import Foundation
import ExternalAccessory

class AccessoryDelegate: NSObject, EAAccessoryDelegate, StreamDelegate {
    var session: EASession?
    var inputBuffer = Data()
    var totalBytesRead = 0
    var outputFile: FileHandle?
    var headerAnalyzed = false

    func startMonitoring() {
        let manager = EAAccessoryManager.shared()

        // Register for accessory notifications
        NotificationCenter.default.addObserver(
            self, selector: #selector(accessoryConnected(_:)),
            name: .EAAccessoryDidConnect, object: nil)
        NotificationCenter.default.addObserver(
            self, selector: #selector(accessoryDisconnected(_:)),
            name: .EAAccessoryDidDisconnect, object: nil)
        manager.registerForLocalNotifications()

        print("[INFO] Waiting for accessories...")
        print("[INFO] Registered protocol: io.grus.exone")

        // Check already connected
        let accessories = manager.connectedAccessories
        print("[INFO] Currently connected accessories: \(accessories.count)")

        for acc in accessories {
            print("[INFO] Found: \(acc.name) by \(acc.manufacturer)")
            print("[INFO]   Model: \(acc.modelNumber)")
            print("[INFO]   Serial: \(acc.serialNumber)")
            print("[INFO]   FW Rev: \(acc.firmwareRevision)")
            print("[INFO]   HW Rev: \(acc.hardwareRevision)")
            print("[INFO]   Protocols: \(acc.protocolStrings)")
            print("[INFO]   Connected: \(acc.isConnected)")

            if acc.protocolStrings.contains("io.grus.exone") {
                openSession(accessory: acc)
            }
        }

        if accessories.isEmpty {
            print("[WARN] No accessories found yet. The device may need a moment after connection.")
            print("[WARN] Or the protocol registration may not be taking effect for CLI tools.")
            print("[INFO] Waiting for connect notification...")
        }
    }

    @objc func accessoryConnected(_ notification: Notification) {
        if let acc = notification.userInfo?[EAAccessoryKey] as? EAAccessory {
            print("[EVENT] Accessory connected: \(acc.name)")
            print("[EVENT] Protocols: \(acc.protocolStrings)")
            if acc.protocolStrings.contains("io.grus.exone") {
                openSession(accessory: acc)
            }
        }
    }

    @objc func accessoryDisconnected(_ notification: Notification) {
        print("[EVENT] Accessory disconnected")
        session = nil
    }

    func openSession(accessory: EAAccessory) {
        print("[SESSION] Opening session with protocol io.grus.exone...")

        guard let eaSession = EASession(accessory: accessory, forProtocol: "io.grus.exone") else {
            print("[ERROR] Failed to create EASession!")
            return
        }

        self.session = eaSession
        print("[SESSION] Session opened successfully!")

        // Set up output file for raw data capture
        let outputPath = "/tmp/teslong_raw_capture.bin"
        FileManager.default.createFile(atPath: outputPath, contents: nil)
        outputFile = FileHandle(forWritingAtPath: outputPath)
        print("[SESSION] Raw data will be saved to \(outputPath)")

        // Configure input stream
        if let inputStream = eaSession.inputStream {
            inputStream.delegate = self
            inputStream.schedule(in: .current, forMode: .default)
            inputStream.open()
            print("[STREAM] Input stream opened")
        } else {
            print("[ERROR] No input stream!")
        }

        // Configure output stream
        if let outputStream = eaSession.outputStream {
            outputStream.delegate = self
            outputStream.schedule(in: .current, forMode: .default)
            outputStream.open()
            print("[STREAM] Output stream opened")
        } else {
            print("[ERROR] No output stream!")
        }
    }

    func stream(_ aStream: Stream, handle eventCode: Stream.Event) {
        switch eventCode {
        case .openCompleted:
            if aStream == session?.inputStream {
                print("[STREAM] Input stream open completed")
            } else {
                print("[STREAM] Output stream open completed")
            }

        case .hasBytesAvailable:
            guard let inputStream = aStream as? InputStream else { return }

            var buffer = [UInt8](repeating: 0, count: 16384)
            let bytesRead = inputStream.read(&buffer, maxLength: buffer.count)

            if bytesRead > 0 {
                let data = Data(buffer[0..<bytesRead])
                totalBytesRead += bytesRead
                inputBuffer.append(data)
                outputFile?.write(data)

                if !headerAnalyzed && inputBuffer.count >= 64 {
                    analyzeData()
                    headerAnalyzed = true
                }

                if totalBytesRead % 65536 < bytesRead {
                    print("[DATA] Total bytes received: \(totalBytesRead) (\(totalBytesRead / 1024) KB)")
                }
            }

        case .hasSpaceAvailable:
            // Output stream ready - we could send commands here
            break

        case .errorOccurred:
            print("[ERROR] Stream error: \(aStream.streamError?.localizedDescription ?? "unknown")")

        case .endEncountered:
            print("[STREAM] End of stream")
            outputFile?.closeFile()
            analyzeCapture()

        default:
            break
        }
    }

    func analyzeData() {
        print("\n[ANALYSIS] First \(inputBuffer.count) bytes received:")

        // Print hex dump of first 128 bytes
        let previewLen = min(128, inputBuffer.count)
        let hexStr = inputBuffer[0..<previewLen].map { String(format: "%02x", $0) }.joined(separator: " ")
        print("[HEX] \(hexStr)")

        // Check for known signatures
        if inputBuffer.count >= 2 {
            let b0 = inputBuffer[0]
            let b1 = inputBuffer[1]

            if b0 == 0xFF && b1 == 0xD8 {
                print("[FORMAT] Detected JPEG stream (FFD8 header)")
            } else if b0 == 0x00 && b1 == 0x00 {
                if inputBuffer.count >= 4 {
                    let b2 = inputBuffer[2]
                    let b3 = inputBuffer[3]
                    if b2 == 0x00 && b3 == 0x01 {
                        print("[FORMAT] Detected H.264 NAL start code (00000001)")
                    }
                }
            } else if b0 == 0x47 {
                print("[FORMAT] Possible MPEG-TS stream (0x47 sync byte)")
            }

            // Print ASCII representation
            let ascii = inputBuffer[0..<previewLen].map {
                (0x20...0x7E).contains($0) ? Character(UnicodeScalar($0)) : Character(".")
            }
            print("[ASCII] \(String(ascii))")
        }
    }

    func analyzeCapture() {
        print("\n[SUMMARY] Total data captured: \(totalBytesRead) bytes")
        print("[SUMMARY] Raw data saved to /tmp/teslong_raw_capture.bin")

        // Look for JPEG frames in the captured data
        var jpegCount = 0
        for i in 0..<(inputBuffer.count - 1) {
            if inputBuffer[i] == 0xFF && inputBuffer[i+1] == 0xD8 {
                jpegCount += 1
            }
        }
        if jpegCount > 0 {
            print("[SUMMARY] Found \(jpegCount) JPEG frame markers in captured data")
        }
    }
}

// Main
print("=== Teslong TD300 Viewer - Protocol Probe ===")
print("Build: \(Date())")
print()

let delegate = AccessoryDelegate()
delegate.startMonitoring()

// Run the event loop
print("[INFO] Running event loop (Ctrl+C to stop)...")
RunLoop.current.run(until: Date(timeIntervalSinceNow: 30))

print("\n[INFO] 30 second timeout reached")
if delegate.totalBytesRead > 0 {
    delegate.analyzeCapture()
} else {
    print("[INFO] No data received from device")
}
