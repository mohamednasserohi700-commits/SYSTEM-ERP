/**
 * ErpProductLinePicker — نظام اختيار الأصناف الذكي
 * متوافق مع app.py الخاص بالمشروع
 *
 * API Endpoints المستخدمة (موجودة بالفعل في app.py):
 *   GET /api/product/search?q=&warehouse_id=
 *   GET /api/product/by_barcode?barcode=&warehouse_id=
 *
 * يعمل في: المبيعات - المشتريات - مرتجع مبيعات - مرتجع مشتريات
 */
(function (global) {
  'use strict';

  /* ══════════════════════════════════════════
     CACHE
  ══════════════════════════════════════════ */
  var _cache = {};
  var CACHE_TTL = 30000;

  function cacheGet(key) {
    var e = _cache[key];
    if (!e) return null;
    if (Date.now() - e.ts > CACHE_TTL) { delete _cache[key]; return null; }
    return e.data;
  }
  function cacheSet(key, data) { _cache[key] = { ts: Date.now(), data: data }; }
  function cacheClear() { _cache = {}; }

  /* ══════════════════════════════════════════
     HTTP GET
  ══════════════════════════════════════════ */
  function get(url, params, cb) {
    var qs = Object.keys(params)
      .filter(function(k){ return params[k] !== '' && params[k] != null; })
      .map(function(k){ return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
      .join('&');
    var full = url + (qs ? '?' + qs : '');
    var cached = cacheGet(full);
    if (cached !== null) { cb(null, cached); return; }
    var xhr = new XMLHttpRequest();
    xhr.open('GET', full, true);
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.onload = function () {
      if (xhr.status === 200) {
        try { var d = JSON.parse(xhr.responseText); cacheSet(full, d); cb(null, d); }
        catch(e) { cb('parse_error'); }
      } else { cb('http_' + xhr.status); }
    };
    xhr.onerror = function () { cb('network_error'); };
    xhr.send();
  }

  /* ══════════════════════════════════════════
     DROPDOWN
  ══════════════════════════════════════════ */
  function renderDropdown(dd, items, query) {
    dd.innerHTML = '';
    if (!items || !items.length) {
      dd.innerHTML = '<div class="plp-empty"><i class="fas fa-search" style="margin-left:6px"></i>لا توجد نتائج</div>';
      dd.style.display = 'block';
      return;
    }
    var frag = document.createDocumentFragment();
    items.forEach(function(item, idx) {
      var el = document.createElement('div');
      el.className = 'plp-item' + (idx === 0 ? ' plp-item--active' : '');
      el.dataset.idx = idx;
      var qty = item.qty != null ? item.qty : null;
      var stockHtml = '';
      if (qty != null) {
        var cls = parseFloat(qty) > 0 ? 'plp-stock--ok' : 'plp-stock--zero';
        stockHtml = '<span class="plp-stock ' + cls + '">' + parseFloat(qty).toFixed(2).replace(/\.00$/,'') + '</span>';
      }
      var price = parseFloat(item.price || 0).toFixed(2);
      el.innerHTML =
        '<span class="plp-name">' + hlText(item.name, query) + '</span>' +
        (item.code ? '<span class="plp-code">' + hlText(item.code, query) + '</span>' : '') +
        (item.unit ? '<span class="plp-unit">' + escHtml(item.unit) + '</span>' : '') +
        '<span class="plp-price">' + price + '</span>' +
        stockHtml;
      frag.appendChild(el);
    });
    dd.appendChild(frag);
    dd._items = items;
    dd._activeIdx = 0;
    dd.style.display = 'block';
  }

  function hlText(text, q) {
    if (!q || !text) return escHtml(text || '');
    return escHtml(text).replace(new RegExp('(' + escReg(q) + ')', 'gi'), '<mark>$1</mark>');
  }
  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function escReg(s) { return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'); }

  /* ══════════════════════════════════════════
     KEYBOARD NAV
  ══════════════════════════════════════════ */
  function moveActive(dd, dir) {
    var items = dd.querySelectorAll('.plp-item');
    if (!items.length) return;
    var cur = parseInt(dd._activeIdx, 10) || 0;
    items[cur] && items[cur].classList.remove('plp-item--active');
    cur = (cur + dir + items.length) % items.length;
    dd._activeIdx = cur;
    items[cur] && items[cur].classList.add('plp-item--active');
    items[cur] && items[cur].scrollIntoView({ block: 'nearest' });
  }

  function getActiveItem(dd) {
    if (!dd._items) return null;
    return dd._items[parseInt(dd._activeIdx, 10) || 0] || null;
  }

  /* ══════════════════════════════════════════
     FILL ROW
     - sell_price → item.price  (المبيعات)
     - cost_price → item.cost   (المشتريات)
  ══════════════════════════════════════════ */
  function fillRow(inp, item, opts) {
    var row = inp.closest('tr');
    if (!row) return;

    var pid = row.querySelector('.pid');
    if (pid) pid.value = item.id;

    inp.value = item.name + (item.code ? ' \u2014 ' + item.code : '');

    var priceInp = row.querySelector('.price');
    if (priceInp) {
      var val = (opts && opts.useCost) ? (item.cost || 0) : (item.price || 0);
      priceInp.value = parseFloat(val).toFixed(2).replace(/\.00$/,'') || '0';
    }

    var qtyInp = row.querySelector('.qty');
    if (qtyInp && (parseFloat(qtyInp.value) || 0) < 0.01) qtyInp.value = '1';

    var availInp = row.querySelector('.avail');
    if (availInp && item.qty != null) {
      availInp.value = parseFloat(item.qty).toFixed(2).replace(/\.00$/,'');
    }

    if (qtyInp && typeof window.calcRow === 'function') window.calcRow(qtyInp);
    if (typeof window.calcTotal === 'function') window.calcTotal();
  }

  /* ══════════════════════════════════════════
     DUPLICATE
  ══════════════════════════════════════════ */
  function findDuplicate(tbody, productId, skipRow) {
    if (!productId || !tbody) return null;
    var rows = tbody.querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] === skipRow) continue;
      var pid = rows[i].querySelector('.pid');
      if (pid && String(pid.value) === String(productId)) return rows[i];
    }
    return null;
  }

  function mergeQty(dupRow) {
    var q = dupRow.querySelector('.qty');
    if (!q) return;
    q.value = (parseFloat(q.value) || 0) + 1;
    if (typeof window.calcRow === 'function') window.calcRow(q);
    if (typeof window.calcTotal === 'function') window.calcTotal();
    dupRow.classList.add('plp-row-flash');
    setTimeout(function(){ dupRow.classList.remove('plp-row-flash'); }, 700);
  }

  /* ══════════════════════════════════════════
     SELECT ITEM
  ══════════════════════════════════════════ */
  function selectItem(inp, dd, item, opts) {
    dd.style.display = 'none';
    dd._items = null;
    var row   = inp.closest('tr');
    var tbody = row && row.closest('tbody');
    var dup   = findDuplicate(tbody, item.id, row);
    if (dup) {
      mergeQty(dup);
      var curPid = row && row.querySelector('.pid');
      if (curPid && !curPid.value) row.remove();
      else { inp.value = ''; if (curPid) curPid.value = ''; }
      return;
    }
    fillRow(inp, item, opts);
    setTimeout(function(){
      var q = row && row.querySelector('.qty');
      if (q) { q.focus(); q.select(); }
    }, 40);
  }

  /* ══════════════════════════════════════════
     BIND SINGLE INPUT
  ══════════════════════════════════════════ */
  function bind(inp, opts) {
    if (!inp || inp._plpBound) return;
    inp._plpBound = true;
    opts = opts || {};
    var dd = inp.parentElement && inp.parentElement.querySelector('.product-dropdown');
    if (!dd) return;
    var timer = null;

    function getWH() { return opts.getWarehouseId ? opts.getWarehouseId() : ''; }

    function doSearch(q) {
      var params = { q: q };
      var wh = getWH();
      if (wh) params.warehouse_id = wh;
      get('/api/product/search', params, function(err, data) {
        if (err) return;
        var arr = Array.isArray(data) ? data : (data.products || []);
        renderDropdown(dd, arr, q);
      });
    }

    inp.addEventListener('input', function() {
      var q = inp.value.trim();
      clearTimeout(timer);
      timer = setTimeout(function(){ doSearch(q); }, 180);
    });

    inp.addEventListener('focus', function() {
      var q = inp.value.trim();
      doSearch(q);
    });

    inp.addEventListener('keydown', function(e) {
      if (dd.style.display === 'none') {
        if (e.key === 'ArrowDown') { doSearch(inp.value.trim() || ''); e.preventDefault(); }
        return;
      }
      if      (e.key === 'ArrowDown')                { moveActive(dd, 1);  e.preventDefault(); }
      else if (e.key === 'ArrowUp')                  { moveActive(dd, -1); e.preventDefault(); }
      else if (e.key === 'Enter' || e.key === 'Tab') {
        var item = getActiveItem(dd);
        if (item) { e.preventDefault(); selectItem(inp, dd, item, opts); }
      }
      else if (e.key === 'Escape') { dd.style.display = 'none'; }
    });

    dd.addEventListener('mousedown', function(e) {
      var el = e.target.closest('.plp-item');
      if (!el) return;
      e.preventDefault();
      dd._activeIdx = parseInt(el.dataset.idx, 10) || 0;
      var item = getActiveItem(dd);
      if (item) selectItem(inp, dd, item, opts);
    });

    document.addEventListener('mousedown', function(e) {
      if (!inp.contains(e.target) && !dd.contains(e.target)) dd.style.display = 'none';
    });
  }

  function bindAll(selector, opts) {
    document.querySelectorAll(selector).forEach(function(i){ bind(i, opts); });
  }

  /* ══════════════════════════════════════════
     BARCODE
     يستخدم /api/product/by_barcode الموجود في app.py
  ══════════════════════════════════════════ */
  var ErpInvoiceBarcode = {
    bind: function(sel, opts) {
      var inp = document.querySelector(sel);
      if (!inp) return;
      opts = opts || {};
      var statusEl = opts.statusSelector ? document.querySelector(opts.statusSelector) : null;

      function showStatus(msg, type) {
        if (!statusEl) return;
        statusEl.textContent = msg;
        statusEl.className = 'erp-barcode-status erp-barcode-status--' + (type || 'info');
        clearTimeout(statusEl._t);
        statusEl._t = setTimeout(function(){ statusEl.textContent = ''; statusEl.className = 'erp-barcode-status'; }, 3500);
      }

      function processCode(code) {
        var wh = opts.getWarehouseId ? opts.getWarehouseId() : '';
        if (opts.requireWarehouse && !wh) { showStatus('يرجى اختيار المخزن أولاً', 'error'); return; }
        showStatus('جاري البحث...', 'info');
        var params = { barcode: code };
        if (wh) params.warehouse_id = wh;
        get('/api/product/by_barcode', params, function(err, data) {
          if (err || !data || data.error) { showStatus('الصنف غير موجود: ' + code, 'error'); return; }
          var item = data;
          var tbody = opts.itemsBodyId ? document.getElementById(opts.itemsBodyId) : null;
          if (!tbody) return;
          var dup = findDuplicate(tbody, item.id, null);
          if (dup) { mergeQty(dup); showStatus('تمت زيادة الكمية: ' + item.name, 'success'); return; }
          var emptyRow = null;
          var rowSel = opts.rowSelector || 'tr';
          tbody.querySelectorAll(rowSel).forEach(function(r){
            if (!emptyRow) { var p = r.querySelector('.pid'); if (p && !p.value) emptyRow = r; }
          });
          if (!emptyRow) {
            if (typeof opts.addRow === 'function') opts.addRow();
            else if (typeof window.addItem      === 'function') window.addItem();
            else if (typeof window.addRow       === 'function') window.addRow();
            else if (typeof window.addReturnRow === 'function') window.addReturnRow();
            var rows = tbody.querySelectorAll(rowSel);
            emptyRow = rows[rows.length - 1];
          }
          if (emptyRow) {
            var si = emptyRow.querySelector('.product-search');
            if (si) fillRow(si, item, opts);
            var dde = emptyRow.querySelector('.product-dropdown');
            if (dde) dde.style.display = 'none';
          }
          showStatus('تمت الإضافة: ' + item.name, 'success');
        });
      }

      inp.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          var code = inp.value.trim();
          if (code) { processCode(code); inp.value = ''; }
        }
      });

      if (opts.autoFocus) setTimeout(function(){ inp.focus(); }, 300);
    }
  };

  /* ══════════════════════════════════════════
     CSS INJECTION
  ══════════════════════════════════════════ */
  (function() {
    if (document.getElementById('plp-styles')) return;
    var s = document.createElement('style');
    s.id = 'plp-styles';
    s.textContent = [
      '@keyframes plpIn{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}',
      '.product-dropdown{display:none;position:absolute;top:calc(100% + 3px);right:0;left:0;',
      'background:var(--secondary,#1e2330);border:1px solid var(--border,#2d3446);',
      'border-radius:10px;z-index:9999;max-height:280px;overflow-y:auto;',
      'box-shadow:0 10px 40px rgba(0,0,0,.4);scrollbar-width:thin;',
      'animation:plpIn .16s ease;}',

      '.product-dropdown::-webkit-scrollbar{width:4px}',
      '.product-dropdown::-webkit-scrollbar-thumb{background:var(--border,#2d3446);border-radius:4px}',

      '.plp-item{display:flex;align-items:center;gap:8px;padding:9px 14px;cursor:pointer;',
      'font-size:13px;border-bottom:1px solid rgba(255,255,255,.04);transition:background .1s;user-select:none}',
      '.plp-item:last-child{border-bottom:none}',
      '.plp-item:hover,.plp-item--active{background:rgba(245,158,11,.13)}',

      '.plp-name{flex:1;font-weight:600;color:var(--text,#e5e7eb);min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.plp-code{font-size:11px;color:var(--text-muted,#8b9ab4);background:rgba(255,255,255,.07);padding:1px 7px;border-radius:4px;font-family:monospace;flex-shrink:0}',
      '.plp-unit{font-size:11px;color:var(--text-muted,#8b9ab4);flex-shrink:0}',
      '.plp-price{font-size:12px;font-weight:700;color:var(--info,#60a5fa);flex-shrink:0}',
      '.plp-stock{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:700;flex-shrink:0}',
      '.plp-stock--ok{background:rgba(16,185,129,.18);color:#10b981}',
      '.plp-stock--zero{background:rgba(239,68,68,.15);color:#ef4444}',
      '.plp-empty{padding:18px;text-align:center;color:var(--text-muted,#8b9ab4);font-size:13px}',
      '.product-dropdown mark{background:rgba(245,158,11,.38);color:inherit;border-radius:2px;padding:0 1px}',

      '@keyframes plp-flash{0%{background:rgba(245,158,11,.35)}100%{background:transparent}}',
      '.plp-row-flash{animation:plp-flash .65s ease-out}',

      '.erp-barcode-status{min-height:28px;padding:5px 12px;font-size:12px;border-radius:6px;margin:4px 8px}',
      '.erp-barcode-status--success{background:rgba(16,185,129,.15);color:#10b981}',
      '.erp-barcode-status--error{background:rgba(239,68,68,.15);color:#ef4444}',
      '.erp-barcode-status--info{background:rgba(96,165,250,.12);color:#60a5fa}',

      '.erp-barcode-wrap{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
      '.erp-barcode-field{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.05);',
      'border:1px solid var(--border,#2d3446);border-radius:8px;padding:4px 10px}',
      '.erp-barcode-field i{color:var(--text-muted,#8b9ab4)}',
      '.erp-barcode-input{border:none!important;background:transparent!important;width:150px;font-size:13px;padding:2px 0!important}',
      '.product-search{direction:rtl}',
    ].join('');
    document.head.appendChild(s);
  })();

  /* ══════════════════════════════════════════
     EXPORTS
  ══════════════════════════════════════════ */
  global.ErpProductLinePicker = { bind: bind, bindAll: bindAll, cacheClear: cacheClear };
  global.ErpInvoiceBarcode    = ErpInvoiceBarcode;

}(window));
