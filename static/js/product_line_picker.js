/**
 * قوائم اختيار الأصناف في فواتير المبيعات/المشتريات/التحويلات — ظهور واضح وبحث فوري
 */
(function () {
    var openPanel = null;
    var scrollHandler = null;

    function esc(s) {
        if (s == null) return '';
        var d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    function hideOpen() {
        if (openPanel) {
            openPanel.style.display = 'none';
            openPanel = null;
        }
        if (scrollHandler) {
            window.removeEventListener('scroll', scrollHandler, true);
            scrollHandler = null;
        }
    }

    function positionPanel(input, panel) {
        var r = input.getBoundingClientRect();
        var w = Math.max(r.width, 280);
        var rtl = document.documentElement.getAttribute('dir') === 'rtl';
        panel.style.position = 'fixed';
        panel.style.top = (r.bottom + 4) + 'px';
        panel.style.width = w + 'px';
        panel.style.maxWidth = 'min(96vw, 520px)';
        if (rtl) {
            panel.style.left = 'auto';
            panel.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
        } else {
            panel.style.right = 'auto';
            panel.style.left = Math.max(8, r.left) + 'px';
        }
        panel.style.maxHeight = 'min(320px, 45vh)';
        panel.style.overflowY = 'auto';
        panel.style.display = 'block';
        panel.style.zIndex = '10050';
        panel.style.background = 'var(--card)';
        panel.style.border = '2px solid var(--accent)';
        panel.style.borderRadius = '10px';
        panel.style.boxShadow = '0 16px 48px rgba(0,0,0,.35)';
    }

    function fetchUrl(q, warehouseId) {
        var params = new URLSearchParams();
        params.set('q', q || '');
        if (warehouseId) params.set('warehouse_id', warehouseId);
        return '/api/product/search?' + params.toString();
    }

    function renderItems(products, panel, input, row, opts) {
        var useCost = opts.useCost;
        if (!products.length) {
            panel.innerHTML = '<div class="erp-pl-empty" style="padding:14px;text-align:center;color:var(--text-muted);font-size:13px;">لا توجد أصناف</div>';
            return;
        }
        panel.innerHTML = products.map(function (p) {
            var price = useCost ? p.cost : p.price;
            var extra = useCost
                ? ('<span style="color:var(--info);">تكلفة: ' + esc(price) + '</span>')
                : ('<span style="color:var(--accent);">بيع: ' + esc(price) + '</span> <span style="color:var(--success);margin-right:8px;">متاح: ' + esc(p.qty) + ' ' + esc(p.unit || '') + '</span>');
            var dn = String(p.name).replace(/"/g, '&quot;');
            return '<div class="erp-pl-item" tabindex="0" data-id="' + p.id + '" data-name="' + dn + '" data-price="' + price + '" data-qty="' + p.qty + '" ' +
                'style="padding:12px 14px;cursor:pointer;font-size:13px;border-bottom:1px solid var(--border);text-align:right;line-height:1.45;">' +
                '<strong>' + esc(p.name) + '</strong> <span style="color:var(--text-muted);font-size:12px;">' + esc(p.code) + '</span>' +
                '<div style="font-size:11px;margin-top:6px;">' + extra + '</div></div>';
        }).join('');

        panel.querySelectorAll('.erp-pl-item').forEach(function (item) {
            function pick() {
                input.value = item.getAttribute('data-name');
                var hid = row.querySelector('.pid') || row.querySelector('.product-id');
                if (hid) hid.value = item.getAttribute('data-id');
                var pr = row.querySelector('.price');
                if (pr) pr.value = item.getAttribute('data-price');
                var av = row.querySelector('.avail') || row.querySelector('.avail-qty');
                if (av) av.value = item.getAttribute('data-qty');
                var qty = row.querySelector('.qty');
                if (qty && row.querySelector('.row-total') && typeof window.calcRow === 'function') {
                    window.calcRow(qty);
                }
                hideOpen();
            }
            item.addEventListener('mousedown', function (e) { e.preventDefault(); pick(); });
            item.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); pick(); } });
        });
    }

    async function loadAndShow(input, panel, row, opts) {
        var wh = opts.getWarehouseId ? opts.getWarehouseId() : '';
        if (opts.requireWarehouse && !wh) {
            panel.innerHTML = '<div style="padding:14px;color:var(--warning);font-size:13px;text-align:center;">اختر المخزن أولاً</div>';
            positionPanel(input, panel);
            openPanel = panel;
            return;
        }
        var q = (input.value || '').trim();
        panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-muted);font-size:13px;">جاري التحميل...</div>';
        positionPanel(input, panel);
        openPanel = panel;
        try {
            var res = await fetch(fetchUrl(q, wh));
            var products = await res.json();
            renderItems(products, panel, input, row, opts);
            positionPanel(input, panel);
        } catch (e) {
            panel.innerHTML = '<div style="padding:14px;color:var(--danger);">خطأ في التحميل</div>';
        }
    }

    window.ErpProductLinePicker = {
        bind: function (input, options) {
            options = options || {};
            var row = input.closest('tr');
            if (!row) return;
            var panel = row.querySelector('.product-dropdown');
            if (!panel) return;
            input.classList.add('erp-product-search');
            panel.classList.add('erp-pl-panel');

            input.addEventListener('focus', function () {
                hideOpen();
                loadAndShow(input, panel, row, options);
                scrollHandler = function () { if (openPanel === panel) positionPanel(input, panel); };
                window.addEventListener('scroll', scrollHandler, true);
            });

            var t = null;
            input.addEventListener('input', function () {
                clearTimeout(t);
                t = setTimeout(function () {
                    loadAndShow(input, panel, row, options);
                    if (!scrollHandler) {
                        scrollHandler = function () { if (openPanel === panel) positionPanel(input, panel); };
                        window.addEventListener('scroll', scrollHandler, true);
                    }
                }, 200);
            });

            document.addEventListener('click', function (e) {
                if (!input.parentNode.contains(e.target) && panel !== e.target && !panel.contains(e.target)) {
                    panel.style.display = 'none';
                    if (openPanel === panel) openPanel = null;
                }
            });
        },
        bindAll: function (selector, options) {
            document.querySelectorAll(selector).forEach(function (inp) {
                window.ErpProductLinePicker.bind(inp, options);
            });
        }
    };
})();