// Small stable helper: secrets travel only through private stdin/stdout pipes.
// Keep the installed executable across app upgrades so its Keychain identity stays stable.
import Foundation
import Security
import LocalAuthentication

func reply(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardOutput.write(data)
    }
}
let input = FileHandle.standardInput.readDataToEndOfFile()
guard input.count <= 16384,
      let body = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any],
      let service = body["service"] as? String,
      service.hasPrefix("local.frank.commoditytrader.credentials."), service.count <= 160,
      let slot = body["slot"] as? String, ["live", "readonly", "model", "selftest"].contains(slot),
      let action = body["action"] as? String else {
    reply(["ok": false, "code": -50]); exit(0)
}
let authentication = LAContext()
authentication.interactionNotAllowed = true
var query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: slot,
    kSecAttrSynchronizable as String: false,
    kSecUseAuthenticationContext as String: authentication
]
var status: OSStatus = errSecParam
if action == "get" {
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { reply(["ok": true, "value": NSNull()]); exit(0) }
    if status == errSecSuccess, let data = result as? Data,
       let value = try? JSONSerialization.jsonObject(with: data) {
        reply(["ok": true, "value": value]); exit(0)
    }
} else if action == "set", let value = body["value"] as? [String: Any],
          let data = try? JSONSerialization.data(withJSONObject: value) {
    status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status == errSecItemNotFound {
        query[kSecValueData as String] = data
        status = SecItemAdd(query as CFDictionary, nil)
    }
} else if action == "delete" {
    status = SecItemDelete(query as CFDictionary)
    if status == errSecItemNotFound { status = errSecSuccess }
}
reply(["ok": status == errSecSuccess, "code": status])
