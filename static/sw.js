/* OilPriceWatch Service Worker：让 H5 可"添加到主屏幕"且能离线看壳。 */
// ⚠️ 改了 static/ 下的任何 SHELL 资源都要把这个版本号 +1，否则老用户
//    一直吃缓存（静态分支是缓存优先），看不到新页面。
//    v3: index.html 增加「网站预测 x 元/吨（由元/升折算）」一行
const CACHE = "oilwatch-v3";
const SHELL = [
  "/",
  "/static/index.html",
  "/static/manifest.webmanifest",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  // 定位相关：静态资源分支是「缓存优先、不写回」，没列进来就永远不会被缓存。
  // 边界数据 54KB，不缓存的话每次打开都要重新下载。
  "/static/provinces.js",
  "/static/geo.js"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) {
    // 接口：网络优先，成功则写回缓存，失败回退缓存（离线也能看上次数据）
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  // 静态资源：缓存优先，缺失再走网络
  event.respondWith(
    caches.match(event.request).then((m) => m || fetch(event.request))
  );
});
