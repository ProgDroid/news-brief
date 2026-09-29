// Event census labelling page (spec 6.3, 6.4; plan Task 6).
//
// Rules this file keeps (plan B6, F19, F24):
// - The DOM is built with createElement/textContent only. The page data is
//   read from the #data JSON block; titles in it are raw text.
// - A link's href is set in exactly one place, externalLink(), and only for
//   an http(s) URL (the server has already dropped every other scheme).
// - One ordered queue carries group creation, assignments, Finish, Abandon
//   and precision answers. Finish waits behind unsaved actions. A network
//   failure or 5xx shows "not saved" and retries in order every 3 s; a 400,
//   403 or 409 stops the queue and the heartbeat for good.
// - The heartbeat is posted only on the blind page, only while it is visible.
// - Every POST is a fetch() in its default "cors" mode. That is load-bearing:
//   the page is served with Referrer-Policy: no-referrer, and for a request
//   whose mode is NOT "cors" (a form post, or fetch with mode "same-origin")
//   the Fetch standard then sends `Origin: null`, which the server refuses.
(function () {
  "use strict";

  var RETRY_MS = 3000;
  var data = JSON.parse(document.getElementById("data").textContent);
  var app = document.getElementById("app");
  var banner = document.getElementById("unsaved");
  var stopped = false;
  var heartbeatTimer = null;

  // ── DOM helpers ───────────────────────────────────────────────────────────

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    var b = el("button", className, label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function externalLink(url) {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return null;
    var a = el("a", "link", "\u{1F517}");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = "open the article";
    a.addEventListener("click", function (event) {
      event.stopPropagation();
    });
    return a;
  }

  function clockTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  // ── Banner and stopping ───────────────────────────────────────────────────

  function showBanner(text, kind) {
    banner.textContent = text;
    banner.className = kind;
    banner.hidden = false;
  }

  function hideBanner() {
    banner.hidden = true;
    banner.textContent = "";
    banner.className = "";
  }

  function stop(text) {
    stopped = true;
    queue.length = 0;
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    document.body.classList.add("stopped");
    showBanner(text, "fatal");
  }

  function stopFor(status) {
    if (status === 409) stop("window closed — reload");
    else if (status === 403) stop("session expired — open a new /label link");
    else stop("not saved: the server refused a change (" + status + ") — reload");
  }

  // ── Transport ─────────────────────────────────────────────────────────────

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      mode: "cors", // see the header comment: not "same-origin"
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      var result = { ok: response.ok, status: response.status, body: null };
      if (response.status !== 200) return result;
      return response.json().then(
        function (parsed) {
          result.body = parsed;
          return result;
        },
        function () {
          return result;
        }
      );
    });
  }

  // ── The ordered queue ─────────────────────────────────────────────────────
  // Each op: { path, body: function () -> object, done: function (json) }.
  // `body` is evaluated at send time so an assignment can name the server id
  // of a group created by an earlier op in the same queue.

  var queue = [];
  var pumping = false;

  function enqueue(op) {
    if (stopped) return;
    queue.push(op);
    pump();
  }

  function pump() {
    if (pumping || stopped || queue.length === 0) return;
    pumping = true;
    var op = queue[0];
    post(op.path, op.body()).then(
      function (result) {
        pumping = false;
        if (stopped) return;
        if (result.ok) {
          queue.shift();
          if (op.done) op.done(result.body);
          if (queue.length === 0) hideBanner();
          pump();
        } else if (result.status >= 500 || result.status === 0) {
          retry();
        } else {
          stopFor(result.status);
        }
      },
      function () {
        pumping = false;
        if (!stopped) retry();
      }
    );
  }

  function retry() {
    showBanner("not saved — retrying", "error");
    setTimeout(pump, RETRY_MS);
  }

  function startHeartbeat() {
    var every = (data.heartbeat_seconds || 60) * 1000;
    heartbeatTimer = setInterval(function () {
      if (stopped || document.visibilityState !== "visible") return;
      post("/api/heartbeat", { window_id: data.window_id }).then(
        function (result) {
          if (result.status === 400 || result.status === 403 || result.status === 409) {
            stopFor(result.status);
          }
        },
        function () {
          // A missed heartbeat is not a lost label: no banner, no retry.
        }
      );
    }, every);
  }

  // ── Tab identity ──────────────────────────────────────────────────────────
  // crypto.randomUUID exists only in secure contexts, and the LAN page is
  // cleartext HTTP (spec 6.2's accepted risk), so fall back to a v4 UUID
  // built from getRandomValues, which is available everywhere.

  function newTabId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      try {
        return window.crypto.randomUUID();
      } catch (e) {
        // fall through
      }
    }
    var b = new Uint8Array(16);
    window.crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map
      .call(b, function (x) {
        return (x + 0x100).toString(16).slice(1);
      })
      .join("");
    return (
      hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) +
      "-" + hex.slice(16, 20) + "-" + hex.slice(20)
    );
  }

  // ── The blind pass ────────────────────────────────────────────────────────

  function blindPage() {
    var windowId = data.window_id;
    var tabId = newTabId();
    var clientSeq = 1;
    var items = data.items;
    var byId = new Map();
    items.forEach(function (item) {
      byId.set(item.id, item);
    });

    var groupOf = new Map(); // item id -> group key | null
    var unsure = new Map(); // item id -> bool
    var groups = new Map(); // group key -> { serverId, touched }
    var touch = 0;
    var localKeys = 0;
    var selected = new Set();
    var finishing = false;

    // After a reload the touch order is not stored; the newest group (the
    // highest server id) is treated as the most recently touched.
    Object.keys(data.assignments || {}).forEach(function (idText) {
      var a = data.assignments[idText];
      var id = Number(idText);
      unsure.set(id, !!a.unsure);
      if (a.group_id === null || a.group_id === undefined) {
        groupOf.set(id, null);
        return;
      }
      var key = "s" + a.group_id;
      groupOf.set(id, key);
      if (!groups.has(key)) groups.set(key, { serverId: a.group_id, touched: a.group_id });
      touch = Math.max(touch, a.group_id);
    });

    function members(key) {
      return items.filter(function (item) {
        return groupOf.get(item.id) === key;
      });
    }

    function groupLabel(key) {
      var m = members(key);
      return m.length ? m[0].title : "";
    }

    function groupHue(key) {
      var h = 0;
      for (var i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) % 360;
      return String(h);
    }

    function liveGroups() {
      var keys = [];
      groups.forEach(function (g, key) {
        if (members(key).length) keys.push(key);
      });
      keys.sort(function (a, b) {
        return groups.get(b).touched - groups.get(a).touched;
      });
      return keys;
    }

    // Layout
    var tools = el("div", "tools");
    var filter = el("input", "filter");
    filter.type = "search";
    filter.placeholder = "filter titles";
    filter.setAttribute("aria-label", "filter titles");
    tools.appendChild(filter);
    app.appendChild(tools);

    var list = el("ol", "items");
    app.appendChild(list);

    var rowOf = new Map();
    items.forEach(function (item) {
      var row = el("li", "row");
      var mark = el("span", "mark");
      var time = el("span", "time", clockTime(item.created_at));
      var outlet = el("span", "outlet", item.outlet);
      var chip = el("span", "chip");
      var title = el("span", "title", item.title);
      row.appendChild(mark);
      row.appendChild(time);
      row.appendChild(outlet);
      row.appendChild(chip);
      row.appendChild(title);
      var link = externalLink(item.url);
      if (link) row.appendChild(link);
      row.addEventListener("click", function () {
        if (finishing || stopped) return;
        if (selected.has(item.id)) selected.delete(item.id);
        else selected.add(item.id);
        refresh();
      });
      list.appendChild(row);
      rowOf.set(item.id, { row: row, mark: mark, chip: chip, item: item });
    });

    var bar = el("div", "bar");
    var count = el("span", "count");
    var newGroup = button("New group", "", onNewGroup);
    var addTo = el("select", "addto");
    addTo.setAttribute("aria-label", "add to group");
    addTo.addEventListener("change", onAddTo);
    var ungroup = button("Ungroup", "", onUngroup);
    var unsureButton = button("Unsure", "", onUnsure);
    var clear = button("Clear", "quiet", function () {
      selected.clear();
      refresh();
    });
    var line1 = el("div", "line");
    [count, newGroup, addTo, ungroup, unsureButton, clear].forEach(function (n) {
      line1.appendChild(n);
    });
    var line2 = el("div", "line");
    var abandonButton = button("Abandon…", "quiet danger", onAbandon);
    var finish = button("Finish blind pass ✓", "primary", onFinish);
    line2.appendChild(abandonButton);
    line2.appendChild(finish);
    bar.appendChild(line1);
    bar.appendChild(line2);
    app.appendChild(bar);

    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      rowOf.forEach(function (r) {
        var hay = (r.item.title + " " + r.item.outlet).toLowerCase();
        r.row.hidden = q !== "" && hay.indexOf(q) === -1;
      });
    });

    function refresh() {
      rowOf.forEach(function (r, id) {
        var key = groupOf.get(id);
        var isUnsure = !!unsure.get(id);
        var isSelected = selected.has(id);
        r.row.classList.toggle("selected", isSelected);
        r.row.classList.toggle("grouped", !!key);
        r.row.classList.toggle("unsure", isUnsure);
        r.mark.textContent = isUnsure ? "?" : isSelected ? "●" : "○";
        if (key) {
          r.chip.hidden = false;
          r.chip.textContent = groupLabel(key);
          r.chip.style.setProperty("--hue", groupHue(key));
        } else {
          r.chip.hidden = true;
          r.chip.textContent = "";
        }
      });

      var n = selected.size;
      count.textContent = "Selected: " + n;
      var locked = finishing || stopped;
      newGroup.disabled = locked || n === 0;
      ungroup.disabled = locked || n === 0;
      unsureButton.disabled = locked || n === 0;
      clear.disabled = n === 0;
      finish.disabled = locked;
      abandonButton.disabled = locked;

      while (addTo.firstChild) addTo.removeChild(addTo.firstChild);
      var placeholder = el("option", "", "Add to…");
      placeholder.value = "";
      addTo.appendChild(placeholder);
      liveGroups().forEach(function (key) {
        var opt = el("option", "", groupLabel(key));
        opt.value = key;
        addTo.appendChild(opt);
      });
      addTo.value = "";
      addTo.disabled = locked || n === 0 || addTo.options.length === 1;
    }

    function saveRows(ids) {
      var seq = clientSeq++;
      var snapshot = ids.map(function (id) {
        return { id: id, key: groupOf.get(id) || null, unsure: !!unsure.get(id) };
      });
      enqueue({
        path: "/api/assign",
        body: function () {
          return {
            window_id: windowId,
            tab_id: tabId,
            client_seq: seq,
            rows: snapshot.map(function (s) {
              return {
                item_id: s.id,
                group_id: s.key === null ? null : groups.get(s.key).serverId,
                unsure: s.unsure,
              };
            }),
          };
        },
      });
    }

    function selection() {
      // Capture order, so the rows of one action are stable.
      return items
        .filter(function (item) {
          return selected.has(item.id);
        })
        .map(function (item) {
          return item.id;
        });
    }

    function moveTo(key) {
      var ids = selection();
      if (!ids.length) return;
      ids.forEach(function (id) {
        groupOf.set(id, key);
      });
      if (key) groups.get(key).touched = ++touch;
      selected.clear();
      saveRows(ids);
      refresh();
    }

    function onNewGroup() {
      if (!selected.size) return;
      var key = "n" + ++localKeys;
      groups.set(key, { serverId: null, touched: ++touch });
      enqueue({
        path: "/api/groups",
        body: function () {
          return { window_id: windowId };
        },
        done: function (json) {
          groups.get(key).serverId = json.group_id;
        },
      });
      moveTo(key);
    }

    function onAddTo() {
      var key = addTo.value;
      if (key && groups.has(key)) moveTo(key);
      else refresh();
    }

    function onUngroup() {
      moveTo(null);
    }

    function onUnsure() {
      var ids = selection();
      if (!ids.length) return;
      var allUnsure = ids.every(function (id) {
        return !!unsure.get(id);
      });
      ids.forEach(function (id) {
        unsure.set(id, !allUnsure);
      });
      selected.clear();
      saveRows(ids);
      refresh();
    }

    function onFinish() {
      if (!window.confirm("Finish the blind pass? Groups cannot be changed after this.")) {
        return;
      }
      finishing = true;
      selected.clear();
      refresh();
      enqueue({
        path: "/api/finish",
        body: function () {
          return { window_id: windowId };
        },
        done: function () {
          window.location.reload();
        },
      });
    }

    function onAbandon() {
      var reason = window.prompt("Abandon this window. Why?");
      if (reason === null) return;
      if (!reason.trim()) {
        window.alert("An abandon needs a reason.");
        return;
      }
      finishing = true;
      selected.clear();
      refresh();
      enqueue({
        path: "/api/abandon",
        body: function () {
          return { window_id: windowId, reason: reason.trim() };
        },
        done: function () {
          window.location.reload();
        },
      });
    }

    refresh();
    startHeartbeat();
  }

  // ── The precision sample ──────────────────────────────────────────────────

  function precisionPage() {
    var byId = new Map();
    data.items.forEach(function (item) {
      byId.set(item.id, item);
    });
    var pairs = data.pairs.slice();
    var index = 0;
    var section = el("section", "precision");
    app.appendChild(section);

    function card(item) {
      var c = el("div", "card");
      var meta = el("div", "meta");
      meta.appendChild(el("span", "time", clockTime(item.created_at)));
      meta.appendChild(el("span", "outlet", item.outlet));
      var link = externalLink(item.url);
      if (link) meta.appendChild(link);
      c.appendChild(meta);
      c.appendChild(el("div", "title", item.title));
      return c;
    }

    function show() {
      while (section.firstChild) section.removeChild(section.firstChild);
      if (index >= pairs.length) {
        section.appendChild(el("p", "note", "All pairs answered."));
        window.location.reload();
        return;
      }
      var pair = pairs[index];
      section.appendChild(
        el("p", "note", "Pair " + (index + 1) + " of " + pairs.length + ": the same occurrence?")
      );
      section.appendChild(card(byId.get(pair[0])));
      section.appendChild(card(byId.get(pair[1])));
      var actions = el("div", "line");
      var same = button("Same", "primary", function () {
        answer("same");
      });
      var different = button("Different", "", function () {
        answer("different");
      });
      actions.appendChild(same);
      actions.appendChild(different);
      section.appendChild(actions);

      function answer(decision) {
        same.disabled = true;
        different.disabled = true;
        enqueue({
          path: "/api/precision",
          body: function () {
            return {
              window_id: data.window_id,
              item_a: pair[0],
              item_b: pair[1],
              decision: decision,
            };
          },
          done: function () {
            index += 1;
            show();
          },
        });
      }
    }

    show();
  }

  // ── Status (nothing to label) ─────────────────────────────────────────────

  function statusPage() {
    var section = el("section", "status");
    section.appendChild(el("p", "", data.detail || data.kind));
    app.appendChild(section);
  }

  if (data.kind === "blind") blindPage();
  else if (data.kind === "precision") precisionPage();
  else statusPage();
})();
