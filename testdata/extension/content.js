chrome.runtime.sendMessage("state", (state) => {
  if (!state?.configured) return;
  const marker = document.createElement("div");
  marker.id = "webcappi-extension-ready";
  marker.dataset.installCount = String(state.installCount);
  marker.textContent = "Extension ready";
  marker.style.cssText = "position:fixed;top:0;left:0;width:100%;height:64px;background:rgb(20,180,60);color:white;font:22px sans-serif;z-index:2147483647";
  document.body.appendChild(marker);
});
