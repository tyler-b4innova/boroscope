#import <Foundation/Foundation.h>
#import <ExternalAccessory/ExternalAccessory.h>
#import <objc/runtime.h>
#import <objc/message.h>

// Declare the private method we want to call
@interface EAAccessoryManager (Private)
- (NSArray *)availableAccessories;
- (void)createEASessionForProtocol:(NSString *)protocol
                     accessoryUUID:(NSString *)uuid
                         withReply:(void (^)(id reply))reply;
- (void)openSessionFromAppToAccessory:(id)info;
- (BOOL)appDeclaresProtocol:(NSString *)protocol;
@end

@interface EAAccessory (Private)
- (NSString *)coreAccessoriesPrimaryUUID;
- (BOOL)createdByCoreAccessories;
- (NSArray *)allPublicProtocolStrings;
- (id)protocolDetails;
@end

@interface EASession (Private)
@property (readonly) unsigned int _sessionID;
@property (readonly) NSString *EASessionUUID;
@property (readonly) int _sock;
@property (readonly) BOOL _useSocketInterfaceForEASession;
@end

int main(int argc, const char **argv) {
    @autoreleasepool {
        printf("=== EA Session Hack ===\n\n");

        EAAccessoryManager *mgr = [EAAccessoryManager sharedAccessoryManager];

        // Wait a moment for the manager to initialize
        [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:2]];

        printf("[MGR] Manager: %s\n", [[mgr description] UTF8String]);

        // Check connected accessories
        NSArray *connected = [mgr connectedAccessories];
        printf("[MGR] Connected accessories: %lu\n", (unsigned long)[connected count]);

        // Try availableAccessories (private method)
        if ([mgr respondsToSelector:@selector(availableAccessories)]) {
            NSArray *available = [mgr availableAccessories];
            printf("[MGR] Available accessories: %lu\n", (unsigned long)[available count]);
            for (EAAccessory *acc in available) {
                printf("  Available: %s by %s\n",
                    [[acc name] UTF8String], [[acc manufacturer] UTF8String]);
                printf("  Protocols: %s\n", [[acc protocolStrings] description].UTF8String);
                if ([acc respondsToSelector:@selector(coreAccessoriesPrimaryUUID)]) {
                    printf("  UUID: %s\n", [[acc coreAccessoriesPrimaryUUID] UTF8String]);
                }
            }
        }

        // Check if protocol is declared
        if ([mgr respondsToSelector:@selector(appDeclaresProtocol:)]) {
            BOOL declared = [mgr appDeclaresProtocol:@"io.grus.exone"];
            printf("[MGR] App declares io.grus.exone: %s\n", declared ? "YES" : "NO");
        }

        // Try to create session directly with a guessed UUID
        // The accessory UUID format from CoreAccessories is typically a UUID string
        printf("\n[SESSION] Attempting to create EA session...\n");

        // First, try getting accessories from the private _connectedAccessories
        if ([mgr respondsToSelector:@selector(_connectedAccessories)]) {
            NSArray *priv = [mgr performSelector:@selector(_connectedAccessories)];
            printf("[MGR] _connectedAccessories: %lu\n", (unsigned long)[priv count]);
        }

        // Try createEASession with various approaches
        if ([mgr respondsToSelector:@selector(createEASessionForProtocol:accessoryUUID:withReply:)]) {
            printf("[SESSION] createEASessionForProtocol available!\n");

            // We don't have the UUID, but let's try with nil or empty
            // to see what error we get
            [mgr createEASessionForProtocol:@"io.grus.exone"
                              accessoryUUID:@""
                                  withReply:^(id reply) {
                printf("[SESSION] Reply: %s\n", [[reply description] UTF8String]);
            }];

            // Pump run loop for the reply
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:3]];
        } else {
            printf("[SESSION] createEASessionForProtocol NOT available\n");
        }

        // Check if there's an internal method to register protocols
        SEL registerSel = NSSelectorFromString(@"registerForLocalNotifications");
        if ([mgr respondsToSelector:registerSel]) {
            [mgr registerForLocalNotifications];
            printf("[MGR] Registered for notifications\n");
        }

        // Pump run loop for any notifications
        printf("\n[WAIT] Pumping run loop for 5 seconds...\n");
        [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:5]];

        // Final check
        connected = [mgr connectedAccessories];
        printf("\n[FINAL] Connected: %lu\n", (unsigned long)[connected count]);

        printf("\n[DONE]\n");
    }
    return 0;
}
