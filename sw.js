const CACHE="piping-qc-v3";
const FILES=["./","index.html","manifest.webmanifest","icon-180.png","icon-192.png","icon-512.png",
"fonts/BarlowCondensed-600.woff2","fonts/BarlowCondensed-700.woff2","fonts/IBMPlexMono-500.woff2","fonts/IBMPlexMono-600.woff2","fonts/IBMPlexSans.woff2"];
self.addEventListener("install",e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES)));self.skipWaiting()});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener("fetch",e=>{
  if(e.request.method!=="GET")return;
  const url=new URL(e.request.url);if(url.origin!==location.origin)return;
  e.respondWith(caches.match(e.request,{ignoreSearch:true}).then(hit=>hit||fetch(e.request).then(res=>{
    if(res.ok){const copy=res.clone();caches.open(CACHE).then(c=>c.put(e.request,copy))}return res
  }).catch(()=>e.request.mode==="navigate"?caches.match("index.html"):undefined)));
});
