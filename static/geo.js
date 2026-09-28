/* 经纬度 → 省级行政区（纯前端，坐标不出设备）。
 *
 * 为什么放前端而不是后端：
 * 浏览器定位拿到的经纬度属于个人位置信息。放前端判定，坐标**从不发往服务器**，
 * 也就不存在收集/存储他人位置数据的问题，同时省掉一次网络往返、可离线用。
 * 代价只是多加载 54KB 边界数据（见 provinces.js，由 tools/build_province_shapes.py 生成）。
 *
 * 算法：逐环射线法（ray casting）+ **命中多个时取面积最小的那个**。
 * 后一条是必需的：这份数据里没有"洞"，直辖市/特别行政区是被邻省轮廓整个盖住的
 * （实测澳门同时落在广东的环内），只取首个命中会把澳门判成广东。
 * 每个环先做 bbox 预筛，实际只有少数几个环需要逐点算。
 *
 * ⚠️ 判定逻辑必须与 tools/build_province_shapes.py 的 locate() 保持一致 ——
 *    后者在生成数据时用同一套城市用例自校验。
 */
(function (root) {
  "use strict";

  var SCALE = 1024;
  var prepared = null; // [{ name, pts: [[lon,lat],...], bbox: [minx,miny,maxx,maxy], area }]

  /** 解开 ECharts 的 ZigZag + delta 压缩串，返回 [[lon, lat], ...]。 */
  function decodeRing(ox, oy, s) {
    var n = s.length >> 1;
    var pts = new Array(n);
    var px = ox;
    var py = oy;
    for (var i = 0, k = 0; k < n; i += 2, k++) {
      var x = s.charCodeAt(i) - 64;
      var y = s.charCodeAt(i + 1) - 64;
      // ZigZag：低位是符号位
      x = (x >> 1) ^ -(x & 1);
      y = (y >> 1) ^ -(y & 1);
      px += x;
      py += y;
      pts[k] = [px / SCALE, py / SCALE];
    }
    return pts;
  }

  function ringArea(pts) {
    var a = 0;
    for (var i = 0, n = pts.length; i < n; i++) {
      var p = pts[i];
      var q = pts[(i + 1) % n];
      a += p[0] * q[1] - q[0] * p[1];
    }
    return Math.abs(a) / 2;
  }

  function prepare() {
    if (prepared) return prepared;
    prepared = [];
    var shapes = root.CN_PROVINCE_SHAPES || {};
    for (var name in shapes) {
      if (!Object.prototype.hasOwnProperty.call(shapes, name)) continue;
      var raw = shapes[name];
      for (var i = 0; i < raw.length; i++) {
        var pts = decodeRing(raw[i][0], raw[i][1], raw[i][2]);
        var minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
        for (var k = 0; k < pts.length; k++) {
          var p = pts[k];
          if (p[0] < minx) minx = p[0];
          if (p[1] < miny) miny = p[1];
          if (p[0] > maxx) maxx = p[0];
          if (p[1] > maxy) maxy = p[1];
        }
        prepared.push({
          name: name,
          pts: pts,
          bbox: [minx, miny, maxx, maxy],
          area: ringArea(pts),
        });
      }
    }
    return prepared;
  }

  /** 射线法：点 (x, y) 是否在环内。 */
  function inRing(x, y, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1];
      var xj = pts[j][0], yj = pts[j][1];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  /**
   * 经纬度 → 省名（如 "浙江"）。定位不到（海上 / 境外）返回 null。
   * ⚠️ 返回的是**省名**而不是 slug：省名→slug 的映射由页面用快照里的 provinces 解析，
   *    避免同一份映射在前后端各维护一遍。
   */
  function locate(lat, lon) {
    if (typeof lat !== "number" || typeof lon !== "number") return null;
    if (!isFinite(lat) || !isFinite(lon)) return null;
    var table = prepare();
    var best = null;
    var bestArea = Infinity;
    for (var i = 0; i < table.length; i++) {
      var r = table[i];
      if (r.area >= bestArea) continue;
      var b = r.bbox;
      if (lon < b[0] || lon > b[2] || lat < b[1] || lat > b[3]) continue;
      if (inRing(lon, lat, r.pts)) {
        best = r.name;
        bestArea = r.area;
      }
    }
    return best;
  }

  root.CNGeo = { locate: locate, decodeRing: decodeRing, _prepare: prepare };

  if (typeof module !== "undefined" && module.exports) module.exports = root.CNGeo;
})(typeof window !== "undefined" ? window : globalThis);
