import Foundation
import ExternalAccessory

// Check if we can reach accessoryd's XPC service directly
print("=== XPC Service Probe ===")

// Method 1: Try ExternalAccessory framework with a run loop
let manager = EAAccessoryManager.shared()

print("[EA] Manager: \(manager)")
print("[EA] Connected accessories: \(manager.connectedAccessories.count)")

// Register for notifications and pump the run loop
NotificationCenter.default.addObserver(
    forName: .EAAccessoryDidConnect, object: nil, queue: nil) { notif in
    print("[EVENT] Accessory connected!")
    if let acc = notif.userInfo?[EAAccessoryKey] as? EAAccessory {
        print("  Name: \(acc.name)")
        print("  Protocols: \(acc.protocolStrings)")
    }
}

manager.registerForLocalNotifications()
print("[EA] Registered for notifications")

// Method 2: Try connecting to the Mach service directly
print("\n=== Direct XPC Connection ===")

// Try connecting to the externalaccessory-server
let connection = NSXPCConnection(machServiceName: "com.apple.accessories.externalaccessory-server")
connection.remoteObjectInterface = nil  // We don't know the protocol yet

// Set up an invalid protocol just to see if we can connect
connection.invalidationHandler = {
    print("[XPC] Connection invalidated")
}
connection.interruptionHandler = {
    print("[XPC] Connection interrupted")
}

print("[XPC] Attempting connection to com.apple.accessories.externalaccessory-server...")
connection.resume()
print("[XPC] Connection resumed (state: active)")

// Give it a moment
Thread.sleep(forTimeInterval: 1)

// Also try the transport server
let transport = NSXPCConnection(machServiceName: "com.apple.accessories.transport-server")
transport.invalidationHandler = {
    print("[XPC-T] Transport connection invalidated")
}
transport.interruptionHandler = {
    print("[XPC-T] Transport connection interrupted")
}
print("[XPC] Attempting connection to com.apple.accessories.transport-server...")
transport.resume()

// Pump run loop for a few seconds to get notifications
print("\n[INFO] Pumping run loop for 5 seconds...")
let deadline = Date(timeIntervalSinceNow: 5)
while Date() < deadline {
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.5))

    // Re-check accessories
    let accs = manager.connectedAccessories
    if !accs.isEmpty {
        print("[EA] Found \(accs.count) accessories!")
        for acc in accs {
            print("  Name: \(acc.name)")
            print("  Manufacturer: \(acc.manufacturer)")
            print("  Protocols: \(acc.protocolStrings)")
            print("  Connected: \(acc.isConnected)")

            // Try opening a session
            if acc.protocolStrings.contains("io.grus.exone") {
                print("  [SESSION] Opening EASession with io.grus.exone...")
                if let session = EASession(accessory: acc, forProtocol: "io.grus.exone") {
                    print("  [SESSION] Success!")
                    if let input = session.inputStream {
                        input.open()
                        print("  [STREAM] Input stream opened")
                        var buf = [UInt8](repeating: 0, count: 4096)
                        Thread.sleep(forTimeInterval: 0.5)
                        if input.hasBytesAvailable {
                            let n = input.read(&buf, maxLength: buf.count)
                            print("  [DATA] Read \(n) bytes!")
                            if n > 0 {
                                let hex = Data(buf[0..<min(64,n)]).map { String(format: "%02x", $0) }.joined(separator: " ")
                                print("  [DATA] \(hex)")
                            }
                        } else {
                            print("  [STREAM] No bytes available yet")
                        }
                    }
                } else {
                    print("  [SESSION] Failed to create EASession")
                }
            }
        }
        break
    }
}

print("\n[EA] Final check: \(manager.connectedAccessories.count) accessories")
print("[DONE]")
