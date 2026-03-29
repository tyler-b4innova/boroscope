import Foundation
import ExternalAccessory
import ObjectiveC

// Inspect ExternalAccessory framework internals at runtime
print("=== ExternalAccessory Runtime Inspection ===\n")

// List all classes in ExternalAccessory framework
let expectedClasses = [
    "EAAccessory", "EAAccessoryManager", "EASession",
    "EAWiFiUnconfiguredAccessory", "EAWiFiUnconfiguredAccessoryBrowser"
]

print("--- Public Classes ---")
for name in expectedClasses {
    if let cls = NSClassFromString(name) {
        print("  \(name): \(cls)")
    }
}

// Try to find private/internal classes
print("\n--- Scanning for EA/ACC private classes ---")
var classCount: UInt32 = 0
let allClasses = objc_copyClassList(&classCount)
if let allClasses = allClasses {
    for i in 0..<Int(classCount) {
        let cls = allClasses[i]
        let name = String(cString: class_getName(cls))
        if name.hasPrefix("EA") || name.hasPrefix("ACC") || name.contains("Accessory") || name.contains("iAP") {
            // Check for interesting methods
            var methodCount: UInt32 = 0
            let methods = class_copyMethodList(cls, &methodCount)
            var methodNames: [String] = []
            if let methods = methods {
                for j in 0..<Int(methodCount) {
                    let sel = method_getName(methods[j])
                    let selName = NSStringFromSelector(sel)
                    if selName.contains("xpc") || selName.contains("XPC") ||
                       selName.contains("session") || selName.contains("Session") ||
                       selName.contains("connect") || selName.contains("Connect") ||
                       selName.contains("open") || selName.contains("Open") ||
                       selName.contains("daemon") || selName.contains("register") ||
                       selName.contains("available") || selName.contains("iap") ||
                       selName.contains("protocol") || selName.contains("Protocol") {
                        methodNames.append(selName)
                    }
                }
                free(methods)
            }
            if !methodNames.isEmpty {
                print("\n  \(name):")
                for m in methodNames.sorted() {
                    print("    - \(m)")
                }
            }
        }
    }
    // allClasses freed automatically
}

// Try to call internal methods on EAAccessoryManager
print("\n--- EAAccessoryManager Internal State ---")
let mgr = EAAccessoryManager.shared()

// Try to access private properties using KVC
let privateKeys = [
    "_connectedAccessories", "_registeredForNotifications",
    "_iapAvailable", "_iap2Available", "delegate",
    "_xpcConnection", "_eaServerConnection"
]
for key in privateKeys {
    do {
        if let value = try (mgr as AnyObject).value(forKey: key) {
            print("  \(key) = \(value)")
        }
    } catch {
        // KVC failed, try selector
    }
}

// Use responds(to:) to check for private methods
let selectors = [
    "_xpcConnection", "_setupXPCConnection", "_connectToServer",
    "_connectToEAServer", "_registerWithDaemon", "_registerWithServer",
    "showBluetoothAccessoryPickerWithNameFilter:completion:",
    "_iapSessionAvailable", "_connectedAccessories",
    "_accessoryForConnectionID:", "_openSessionForAccessory:protocol:",
]
print("\n--- Selector Availability ---")
for selName in selectors {
    let sel = Selector(selName)
    let responds = mgr.responds(to: sel)
    if responds {
        print("  ✓ \(selName)")
    }
}

// Check if we can see the XPC connection details
print("\n--- Checking XPC Service Connection ---")

// Try to talk to accessoryd directly using distributed objects or XPC
// First, let's see if NSConnection works
print("  Attempting raw XPC to com.apple.accessories.externalaccessory-server...")
let xpc = NSXPCConnection(machServiceName: "com.apple.accessories.externalaccessory-server")
xpc.invalidationHandler = { print("  [XPC] Invalidated") }
xpc.interruptionHandler = { print("  [XPC] Interrupted") }
xpc.resume()
print("  [XPC] Connection active")

// Try sending a ping
Thread.sleep(forTimeInterval: 0.5)

// Check endpoint info
print("  [XPC] endpoint: \(xpc.endpoint)")
print("  [XPC] serviceName: \(xpc.serviceName ?? "nil")")

// Also check transport server
let xpc2 = NSXPCConnection(machServiceName: "com.apple.accessories.transport-server")
xpc2.resume()
Thread.sleep(forTimeInterval: 0.5)

print("\n[DONE]")
