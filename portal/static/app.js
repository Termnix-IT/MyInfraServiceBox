// 状態の表示部分だけを一定間隔で取り直して差し替える。
(() => {
  const root = document.getElementById("status");
  if (!root) return;
  const seconds = Number(root.dataset.refreshSeconds) || 30;

  async function refresh() {
    try {
      const response = await fetch("/partials/status", { cache: "no-store" });
      if (response.ok) root.innerHTML = await response.text();
    } catch {
      // ポータル自体が再起動中などで取れないときは、次の周期でもう一度試す
    }
  }

  setInterval(refresh, seconds * 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
})();
