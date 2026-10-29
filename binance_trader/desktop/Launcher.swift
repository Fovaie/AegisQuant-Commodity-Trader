import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var backend: Process?
    var attempts = 0
    let endpoint = URL(string: "http://127.0.0.1:8766")!
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu(); let item = NSMenuItem(); menu.addItem(item)
        let appMenu = NSMenu(); item.submenu = appMenu
        appMenu.addItem(withTitle: "Show AegisQuant", action: #selector(showWindow), keyEquivalent: "0")
        appMenu.addItem(withTitle: "API Connection Storage…", action: #selector(configureCredentialStorage), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit AegisQuant", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); menu.addItem(editItem)
        let edit = NSMenu(title: "Edit"); editItem.submenu = edit
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1200,height: 820), styleMask: [.titled,.closable,.miniaturizable,.resizable], backing: .buffered, defer: false)
        window.title = "AegisQuant"; window.minSize = NSSize(width: 800,height: 600)
        window.isReleasedWhenClosed = false
        web = WKWebView(); web.navigationDelegate = self; window.contentView = web
        window.center(); showWindow()
        web.loadHTMLString("<body style='font:22px -apple-system;padding:60px;background:#f4f7f6'>Starting AegisQuant…</body>", baseURL: nil)
        probe(startIfNeeded: true)
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func configureCredentialStorage() {
        let choice = NSAlert()
        choice.messageText = "API Connection Storage"
        choice.informativeText = "If enabled, future Binance and model API connections are saved in macOS Keychain. Restart restores connections with read-only verification; new entries remain paused. This preference takes effect on the next backend start and does not restart the current service. Session-only mode does not remove previously saved keys; clear them on the connection page."
        choice.addButton(withTitle: "Cancel")
        choice.addButton(withTitle: "Save to Keychain")
        choice.addButton(withTitle: "This Session Only")
        let answer = choice.runModal()
        if answer != .alertFirstButtonReturn {
            UserDefaults.standard.set(answer == .alertSecondButtonReturn, forKey: "saveAPIConnections")
        }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func probe(startIfNeeded: Bool) {
        var req = URLRequest(url: endpoint.appendingPathComponent("api/state")); req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { data, response, error in
            DispatchQueue.main.async {
                if let data = data, let value = (try? JSONSerialization.jsonObject(with: data)) as? [String:Any], value["state"] != nil,
                   value["application"] as? String == "commodity-strategy-terminal",
                   ["paper", "live"].contains(value["mode"] as? String ?? "") {
                    self.web.load(URLRequest(url: self.endpoint)); return
                }
                if startIfNeeded { self.startBackend() }
                self.attempts += 1
                if self.attempts >= 120 { self.fail("The local service could not start. Check desktop.log in the data directory and confirm that port 8766 is available."); return }
                DispatchQueue.main.asyncAfter(deadline: .now()+1) { self.probe(startIfNeeded: false) }
            }
        }.resume()
    }
    func startBackend() {
        guard let resources = Bundle.main.resourceURL,
              let path = try? String(contentsOf: resources.appendingPathComponent("data-path.txt"), encoding: .utf8) else { fail("Application configuration not found."); return }
        let data = URL(fileURLWithPath: path.trimmingCharacters(in: .whitespacesAndNewlines))
        do {
            try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
            let log = data.appendingPathComponent("desktop.log")
            if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            let handle = try FileHandle(forWritingTo: log); handle.seekToEndOfFile()
            let p = Process(); p.executableURL = resources.appendingPathComponent("backend/commodity-backend")
            p.arguments = ["--port", "8766"]
            var env = ProcessInfo.processInfo.environment; env["BINANCE_TRADER_DATA"] = data.path
            let preferences = UserDefaults.standard
            if preferences.object(forKey: "saveAPIConnections") == nil {
                let choice = NSAlert()
                choice.messageText = "Save API connections in macOS Keychain?"
                choice.informativeText = "Save future Binance and model API keys in local Keychain. Restart restores connections through read-only verification; new entries remain paused. Session-only mode requires entering keys again after quitting."
                choice.addButton(withTitle: "This Session Only")
                choice.addButton(withTitle: "Save to Keychain")
                preferences.set(choice.runModal() == .alertSecondButtonReturn, forKey: "saveAPIConnections")
            }
            env["COMMODITY_SAVE_CREDENTIALS"] = preferences.bool(forKey: "saveAPIConnections") ? "1" : "0"
            p.environment = env; p.standardOutput = handle; p.standardError = handle
            try p.run(); backend = p
        } catch { fail("Startup failed: \(error.localizedDescription)") }
    }
    func fail(_ message: String) { let alert = NSAlert(); alert.messageText = "Unable to Start AegisQuant"; alert.informativeText = message; alert.runModal() }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        // External links open only after a deliberate click, outside the app.
        if navigationAction.navigationType == .linkActivated && url.scheme == "https" &&
           url.host != nil && url.user == nil && url.password == nil {
            NSWorkspace.shared.open(url); decisionHandler(.cancel); return
        }
        decisionHandler((url.scheme == "http" && url.host == "127.0.0.1" && url.port == 8766) || url.scheme == "about" ? .allow : .cancel)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let p = backend, p.isRunning {
            let alert = NSAlert(); alert.messageText = "Quit AegisQuant?"
            alert.informativeText = "Quitting stops background strategies, simulated stops, and live status monitoring. Confirmed exchange protective orders remain; verify unknown orders at Binance. Close only the window to keep the backend running. New entries remain paused after restart, and unsaved API keys must be entered again."
            alert.addButton(withTitle: "Cancel"); alert.addButton(withTitle: "Quit App")
            if alert.runModal() != .alertSecondButtonReturn { return .terminateCancel }
            p.terminate()
        }
        return .terminateNow
    }
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular); app.delegate = delegate; app.run()
