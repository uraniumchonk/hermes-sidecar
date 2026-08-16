// Meow Agent Panel - background service worker
// 唯一職責：點工具列圖示時打開原生側邊欄

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});

// 確保每次點擊都會開啟（有些 Chrome 版本 onInstalled 後行為會重置）
chrome.runtime.onStartup.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
