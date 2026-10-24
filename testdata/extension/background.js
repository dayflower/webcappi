chrome.runtime.onInstalled.addListener(async ({reason}) => {
  if (reason === "install") {
    await chrome.storage.local.set({configured: true, installCount: 1});
    await chrome.tabs.create({url: chrome.runtime.getURL("welcome.html")});
  }
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message === "state") {
    // Deliberately focus an unrelated tab before the capture target is ready.
    chrome.tabs.create({url: chrome.runtime.getURL("welcome.html")}).then(() => {
      chrome.storage.local.get(["configured", "installCount"]).then(reply);
    });
    return true;
  }
});
