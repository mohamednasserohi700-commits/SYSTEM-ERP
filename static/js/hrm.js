/* ProERP — HRM Enterprise UI Helpers */
(function () {
    'use strict';

    /* ── Confirm Dialog ── */
    window.hrmConfirm = function (opts) {
        opts = opts || {};
        var overlay = document.getElementById('hrmConfirmOverlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'hrmConfirmOverlay';
            overlay.className = 'hrm-confirm-overlay';
            overlay.innerHTML = '<div class="hrm-confirm-box"><h4 id="hrmConfirmTitle"></h4><p id="hrmConfirmMsg"></p><div class="hrm-confirm-actions"><button type="button" class="btn btn-outline" id="hrmConfirmCancel">إلغاء</button><button type="button" class="btn btn-danger" id="hrmConfirmOk">تأكيد</button></div></div>';
            document.body.appendChild(overlay);
        }
        document.getElementById('hrmConfirmTitle').textContent = opts.title || 'تأكيد';
        document.getElementById('hrmConfirmMsg').textContent = opts.message || 'هل أنت متأكد؟';
        var okBtn = document.getElementById('hrmConfirmOk');
        okBtn.className = 'btn ' + (opts.danger !== false ? 'btn-danger' : 'btn-primary');
        okBtn.textContent = opts.okText || 'تأكيد';
        overlay.classList.add('open');
        return new Promise(function (resolve) {
            function close(val) {
                overlay.classList.remove('open');
                document.getElementById('hrmConfirmCancel').onclick = null;
                okBtn.onclick = null;
                resolve(val);
            }
            document.getElementById('hrmConfirmCancel').onclick = function () { close(false); };
            okBtn.onclick = function () { close(true); };
        });
    };

    /* ── Skeleton ── */
    window.hrmShowSkeleton = function (container, rows) {
        if (!container) return;
        rows = rows || 5;
        var html = '';
        for (var i = 0; i < rows; i++) html += '<div class="hrm-skeleton hrm-skeleton-row"></div>';
        container.innerHTML = html;
    };

    /* ── Enterprise DataGrid ── */
    window.HrmDataGrid = function (table, options) {
        if (!table) return;
        options = options || {};
        var wrap = table.closest('.hrm-datagrid-wrap') || table.parentElement;
        table.classList.add('hrm-datagrid');
        var sortCol = -1, sortDir = 1;

        /* Column resize */
        table.querySelectorAll('thead th').forEach(function (th, idx) {
            if (th.classList.contains('no-sort') || th.dataset.noresize) return;
            var resizer = document.createElement('div');
            resizer.className = 'col-resizer';
            th.style.position = 'relative';
            th.insertBefore(resizer, th.firstChild);
            var startX, startW;
            resizer.addEventListener('mousedown', function (e) {
                e.stopPropagation();
                startX = e.pageX;
                startW = th.offsetWidth;
                document.onmousemove = function (ev) {
                    var w = startW + (ev.pageX - startX);
                    th.style.width = Math.max(60, w) + 'px';
                };
                document.onmouseup = function () { document.onmousemove = null; document.onmouseup = null; };
            });
            if (!th.classList.contains('no-sort')) {
                th.addEventListener('click', function (e) {
                    if (e.target.classList.contains('col-resizer')) return;
                    sortTable(idx, th);
                });
            }
        });

        function sortTable(colIdx, thEl) {
            var tbody = table.querySelector('tbody');
            var rows = Array.from(tbody.querySelectorAll('tr')).filter(function (r) { return !r.querySelector('.empty-state'); });
            if (sortCol === colIdx) sortDir *= -1;
            else { sortCol = colIdx; sortDir = 1; }
            table.querySelectorAll('thead th').forEach(function (t) { t.classList.remove('sort-asc', 'sort-desc'); });
            thEl.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
            rows.sort(function (a, b) {
                var av = (a.cells[colIdx] || {}).textContent || '';
                var bv = (b.cells[colIdx] || {}).textContent || '';
                var an = parseFloat(av.replace(/,/g, ''));
                var bn = parseFloat(bv.replace(/,/g, ''));
                if (!isNaN(an) && !isNaN(bn)) return (an - bn) * sortDir;
                return av.localeCompare(bv, 'ar') * sortDir;
            });
            rows.forEach(function (r) { tbody.appendChild(r); });
        }

        /* Column hide menu */
        var colMenu = wrap.querySelector('.hrm-col-menu');
        if (colMenu) {
            colMenu.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
                cb.addEventListener('change', function () {
                    var idx = parseInt(cb.dataset.col, 10);
                    table.querySelectorAll('tr').forEach(function (row) {
                        var cell = row.cells[idx];
                        if (cell) cell.classList.toggle('col-hidden', !cb.checked);
                    });
                    var th = table.querySelectorAll('thead th')[idx];
                    if (th) th.classList.toggle('col-hidden', !cb.checked);
                });
            });
        }

        /* Bulk selection */
        var bulkBar = wrap.querySelector('.hrm-bulk-bar');
        var masterCb = table.querySelector('.hrm-select-all');
        if (masterCb) {
            masterCb.addEventListener('change', function () {
                table.querySelectorAll('.hrm-row-select').forEach(function (cb) {
                    cb.checked = masterCb.checked;
                    cb.closest('tr').classList.toggle('selected', cb.checked);
                });
                updateBulkBar();
            });
            table.querySelectorAll('.hrm-row-select').forEach(function (cb) {
                cb.addEventListener('change', function () {
                    cb.closest('tr').classList.toggle('selected', cb.checked);
                    updateBulkBar();
                });
            });
        }
        function updateBulkBar() {
            var n = table.querySelectorAll('.hrm-row-select:checked').length;
            if (bulkBar) {
                bulkBar.classList.toggle('visible', n > 0);
                var cnt = bulkBar.querySelector('.hrm-bulk-count');
                if (cnt) cnt.textContent = n;
            }
        }

        /* Client filter */
        var filterInput = wrap.querySelector('.hrm-client-filter');
        if (filterInput) {
            filterInput.addEventListener('input', function () {
                var q = filterInput.value.trim().toLowerCase();
                table.querySelectorAll('tbody tr').forEach(function (row) {
                    if (row.querySelector('.empty-state')) return;
                    row.style.display = !q || row.textContent.toLowerCase().includes(q) ? '' : 'none';
                });
            });
        }

        /* Export Excel (CSV) */
        var exportBtn = wrap.querySelector('[data-export="excel"]');
        if (exportBtn) {
            exportBtn.addEventListener('click', function () {
                if (options.exportUrl) {
                    window.location.href = options.exportUrl;
                    return;
                }
                exportTableCSV(table, options.filename || 'export.csv');
            });
        }

        /* Export PDF (print) */
        var pdfBtn = wrap.querySelector('[data-export="pdf"]');
        if (pdfBtn) {
            pdfBtn.addEventListener('click', function () {
                if (options.pdfUrl) {
                    window.open(options.pdfUrl, '_blank');
                    return;
                }
                printTable(table, options.printTitle || document.title);
            });
        }
    };

    function exportTableCSV(table, filename) {
        var rows = [];
        table.querySelectorAll('tr').forEach(function (tr) {
            if (tr.style.display === 'none') return;
            var cells = tr.querySelectorAll('th, td');
            var row = [];
            cells.forEach(function (c) {
                if (c.classList.contains('col-hidden') || c.querySelector('input[type=checkbox]')) return;
                row.push('"' + (c.textContent || '').trim().replace(/"/g, '""') + '"');
            });
            if (row.length) rows.push(row.join(','));
        });
        var blob = new Blob(['\ufeff' + rows.join('\n')], { type: 'text/csv;charset=utf-8;' });
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        if (typeof showToast === 'function') showToast('تم تصدير Excel', 'success');
    }

    function printTable(table, title) {
        var win = window.open('', '_blank');
        var html = '<html dir="rtl"><head><title>' + title + '</title><style>body{font-family:Cairo,sans-serif;padding:20px}table{width:100%;border-collapse:collapse}th,td{border:1px solid #ccc;padding:8px;font-size:12px}th{background:#f5f5f5}</style></head><body><h2>' + title + '</h2>' + table.outerHTML + '</body></html>';
        win.document.write(html);
        win.document.close();
        win.onload = function () { win.print(); };
    }

    /* ── View mode toggle (table / cards) ── */
    window.hrmInitViewToggle = function () {
        var toggles = document.querySelectorAll('[data-hrm-view]');
        var tableView = document.getElementById('hrmTableView');
        var cardView = document.getElementById('hrmCardView');
        if (!tableView || !cardView) return;
        toggles.forEach(function (btn) {
            btn.addEventListener('click', function () {
                var mode = btn.dataset.hrmView;
                toggles.forEach(function (b) { b.classList.toggle('active', b === btn); });
                tableView.style.display = mode === 'table' ? '' : 'none';
                cardView.style.display = mode === 'cards' ? '' : 'none';
                try { localStorage.setItem('hrm_emp_view', mode); } catch (e) {}
            });
        });
        try {
            var saved = localStorage.getItem('hrm_emp_view');
            if (saved) document.querySelector('[data-hrm-view="' + saved + '"]')?.click();
        } catch (e) {}
    };

    /* ── Quick search debounce ── */
    window.hrmQuickSearch = function (input, form) {
        if (!input || !form) return;
        var timer;
        input.addEventListener('input', function () {
            clearTimeout(timer);
            timer = setTimeout(function () { form.submit(); }, 400);
        });
    };

    /* ── Bulk action handler ── */
    window.hrmBulkAction = function (form, action) {
        var table = document.querySelector('.hrm-datagrid');
        if (!table) return;
        var ids = [];
        table.querySelectorAll('.hrm-row-select:checked').forEach(function (cb) {
            ids.push(cb.value);
        });
        if (!ids.length) {
            if (typeof showToast === 'function') showToast('اختر موظفاً واحداً على الأقل', 'warning');
            return;
        }
        hrmConfirm({ title: 'تأكيد العملية', message: 'تطبيق "' + action + '" على ' + ids.length + ' موظف(ين)؟' }).then(function (ok) {
            if (!ok) return;
            var fd = new FormData(form);
            fd.set('action', action);
            ids.forEach(function (id) { fd.append('employee_ids', id); });
            fetch(form.action, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'XMLHttpRequest' } })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (typeof showToast === 'function') showToast(d.message || 'تم', d.ok ? 'success' : 'error');
                    if (d.ok) setTimeout(function () { location.reload(); }, 800);
                })
                .catch(function () { if (typeof showToast === 'function') showToast('خطأ في الاتصال', 'error'); });
        });
    };

    /* ── Column picker toggle ── */
    document.addEventListener('click', function (e) {
        var picker = e.target.closest('[data-col-picker]');
        var menus = document.querySelectorAll('.hrm-col-menu');
        if (picker) {
            var menu = picker.parentElement.querySelector('.hrm-col-menu');
            menus.forEach(function (m) { if (m !== menu) m.classList.remove('open'); });
            if (menu) menu.classList.toggle('open');
            e.stopPropagation();
            return;
        }
        menus.forEach(function (m) { m.classList.remove('open'); });
    });

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.hrm-datagrid').forEach(function (tbl) {
            var wrap = tbl.closest('.hrm-datagrid-wrap');
            var opts = {};
            if (wrap) {
                opts.exportUrl = wrap.dataset.exportUrl || '';
                opts.pdfUrl = wrap.dataset.pdfUrl || '';
                opts.filename = wrap.dataset.filename || 'export.csv';
                opts.printTitle = wrap.dataset.printTitle || '';
            }
            HrmDataGrid(tbl, opts);
        });
        hrmInitViewToggle();
        document.querySelectorAll('.hrm-quick-search input[name=q]').forEach(function (inp) {
            var form = inp.closest('form');
            if (form) hrmQuickSearch(inp, form);
        });
        document.querySelectorAll('form[data-hrm-confirm]').forEach(function (form) {
            form.addEventListener('submit', function (e) {
                if (form.dataset.confirmed) return;
                e.preventDefault();
                hrmConfirm({ title: form.dataset.confirmTitle || 'تأكيد', message: form.dataset.confirmMsg || 'هل أنت متأكد؟', danger: form.dataset.confirmDanger !== '0' })
                    .then(function (ok) {
                        if (ok) { form.dataset.confirmed = '1'; form.submit(); }
                    });
            });
        });
    });
})();
