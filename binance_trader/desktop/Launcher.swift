import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var backend: Process?
    var attempts = 0
    let edition = (try? String(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("edition.txt"), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)) ?? "en"
    func text(_ en: String, _ zh: String) -> String { edition == "zh-CN" ? zh : en }
    let endpoint = URL(string: "http://127.0.0.1:8766")!
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu(); let item = NSMenuItem(); menu.addItem(item)
        let appMenu = NSMenu(); item.submenu = appMenu
        appMenu.addItem(withTitle: text("Show AegisQuant Commodity Trader", "显示商品交易终端"), action: #selector(showWindow), keyEquivalent: "0")
        appMenu.addItem(withTitle: text("API Connection Storage…", "API 连接保存…"), action: #selector(configureCredentialStorage), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: text("Quit AegisQuant Commodity Trader", "退出商品交易终端"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let editItem = NSMenuItem(); menu.addItem(editItem)
        let edit = NSMenu(title: text("Edit", "编辑")); editItem.submenu = edit
        edit.addItem(withTitle: text("Cut", "剪切"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: text("Copy", "复制"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: text("Paste", "粘贴"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: text("Select All", "全选"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0,y: 0,width: 1200,height: 820), styleMask: [.titled,.closable,.miniaturizable,.resizable], backing: .buffered, defer: false)
        window.title = text("AegisQuant Commodity Trader EN", "AegisQuant 商品交易终端 中文版"); window.minSize = NSSize(width: 800,height: 600)
        window.isReleasedWhenClosed = false
        web = WKWebView(); web.navigationDelegate = self; window.contentView = web
        window.center(); showWindow()
        web.loadHTMLString("<body style='font:14px -apple-system;padding:25px;background:#e6e9ed'>\(text("Starting trading terminal…", "正在启动交易终端…"))</body>", baseURL: nil)
        probe(startIfNeeded: true)
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func configureCredentialStorage() {
        let choice = NSAlert()
        choice.messageText = text("API Connection Storage", "API 连接保存")
        choice.informativeText = text("If enabled, future Binance and model API connections are saved in macOS Keychain. Restart restores connections with read-only verification; new entries remain paused. This preference takes effect on the next backend start and does not restart the current service. Session-only mode does not remove previously saved keys; clear them on the connection page.", "启用后，新连接的币安及模型密钥会保存到本机钥匙串。重启时只读核验并恢复连接，新开仓保持暂停。设置在下次后台启动时生效，不会重启当前服务。仅本次运行模式不会删除已有密钥，请在连接页清除。")
        choice.addButton(withTitle: text("Cancel", "取消"))
        choice.addButton(withTitle: text("Save to Keychain", "保存到钥匙串"))
        choice.addButton(withTitle: text("This Session Only", "仅本次运行"))
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
                    if value["edition_language"] as? String != self.edition {
                        self.fail(self.text("Another edition is running on port 8766. Quit that edition before opening this one. Do not interrupt a running trading strategy without checking positions.", "8766 端口上正在运行另一语言版本。请核对持仓并正常退出该版本，再打开本版本。")); return
                    }
                    self.web.load(URLRequest(url: self.endpoint)); return
                }
                if startIfNeeded { self.startBackend() }
                self.attempts += 1
                if self.attempts >= 120 { self.fail(self.text("The local service could not start. Check desktop.log in the data directory and confirm that port 8766 is available.", "本地服务无法启动。请检查数据目录中的 desktop.log，并确认 8766 端口可用。")); return }
                DispatchQueue.main.asyncAfter(deadline: .now()+1) { self.probe(startIfNeeded: false) }
            }
        }.resume()
    }
    func startBackend() {
        guard let resources = Bundle.main.resourceURL,
              let path = try? String(contentsOf: resources.appendingPathComponent("data-path.txt"), encoding: .utf8) else { fail(text("Application configuration not found.", "未找到应用配置。")); return }
        let savedPath = path.trimmingCharacters(in: .whitespacesAndNewlines)
        let data = savedPath == "@APPLICATION_SUPPORT@" ? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("CommodityTrader") : URL(fileURLWithPath: savedPath)
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
                choice.messageText = text("Save API connections in macOS Keychain?", "将 API 连接保存到 macOS 钥匙串？")
                choice.informativeText = text("Save future Binance and model API keys in local Keychain. Restart restores connections through read-only verification; new entries remain paused. Session-only mode requires entering keys again after quitting.", "将今后输入的币安及模型密钥保存在本机钥匙串，重启后通过只读核验恢复连接，新开仓保持暂停。仅本次运行时，退出后需重新输入密钥。")
                choice.addButton(withTitle: text("This Session Only", "仅本次运行"))
                choice.addButton(withTitle: text("Save to Keychain", "保存到钥匙串"))
                preferences.set(choice.runModal() == .alertSecondButtonReturn, forKey: "saveAPIConnections")
            }
            env["COMMODITY_SAVE_CREDENTIALS"] = preferences.bool(forKey: "saveAPIConnections") ? "1" : "0"
            p.environment = env; p.standardOutput = handle; p.standardError = handle
            try p.run(); backend = p
        } catch { fail("Startup failed: \(error.localizedDescription)") }
    }
    func fail(_ message: String) { let alert = NSAlert(); alert.messageText = text("Unable to Start AegisQuant Commodity Trader", "无法启动商品交易终端"); alert.informativeText = message; alert.runModal() }
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
            let alert = NSAlert(); alert.messageText = text("Quit AegisQuant Commodity Trader?", "退出商品交易终端？")
            alert.informativeText = text("Quitting stops background strategies, simulated stops, and live status monitoring. Confirmed exchange protective orders remain; verify unknown orders at Binance. Close only the window to keep the backend running. New entries remain paused after restart, and unsaved API keys must be entered again.", "退出会停止后台策略、模拟止损及实盘状态监控。交易所已确认的保护单保留；结果不明的订单须在币安核对。仅关闭窗口可让后台继续运行。重启后新开仓暂停，未保存的密钥需重新输入。")
            alert.addButton(withTitle: text("Cancel", "取消")); alert.addButton(withTitle: text("Quit App", "退出应用"))
            if alert.runModal() != .alertSecondButtonReturn { return .terminateCancel }
            p.terminate()
        }
        return .terminateNow
    }
}
let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.regular); app.delegate = delegate; app.run()
