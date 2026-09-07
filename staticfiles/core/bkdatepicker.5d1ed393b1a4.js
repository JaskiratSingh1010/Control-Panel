/* ============================================================================
   bkdatepicker - our own calendar and our own dropdown list
   ----------------------------------------------------------------------------
   WHAT IT DOES
     The browser draws its own calendar for <input type="date"> / <type="month">
     and its own grey list for <select>. Neither can be styled, so they never
     look like the rest of the panel. This script stops those two popups from
     opening and shows ours instead. The looks live in bkdatepicker.css.

   WHAT IT DOES NOT DO  (important)
     It never removes, replaces or rebuilds an element. Every <input> and every
     <select> stays exactly where it is, with the same id, name, type and value.
     When the reader picks something we write it back to the real element and
     fire the normal 'input' and 'change' events - so every bit of page code
     that already reads .value or listens for change keeps working untouched.
     Nothing else on any page had to change for this.

   TURNING IT OFF for one element:  <input type="date" data-bkdp="off">
   ========================================================================== */
(function () {
  'use strict';
  if (window.__bkdpReady) return;          // never wire the page twice
  window.__bkdpReady = true;

  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
                'July', 'August', 'September', 'October', 'November', 'December'];
  var MON3 = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DOW = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  // Tell the page the value changed, exactly as the browser would have.
  function fire(node) {
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function dayKey(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function monKey(y, m) { return y + '-' + pad(m + 1); }
  function today() { var d = new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }

  // "2026-08-31" (or "2026-08") -> {y, m, d}.  Anything else -> null.
  function parseVal(s) {
    var m = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(String(s || '').trim());
    if (!m) return null;
    return { y: +m[1], m: +m[2] - 1, d: m[3] ? +m[3] : 1 };
  }

  /* min / max on the input are ISO strings, and ISO dates sort correctly as
     plain text, so a string compare is all the range check needs. */
  function limits(input) {
    return { min: (input.getAttribute('min') || '').trim(),
             max: (input.getAttribute('max') || '').trim() };
  }
  function allowed(input, key, len) {
    var L = limits(input);
    if (L.min && key < L.min.slice(0, len)) return false;
    if (L.max && key > L.max.slice(0, len)) return false;
    return true;
  }

  // ── one popup element, reused by every field on the page ──────────────────
  var pop = null, host = null, mode = 'date', view = null, page = 'days';

  function popup() {
    if (pop) return pop;
    pop = document.createElement('div');
    pop.className = 'bkdp';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Choose a date');
    pop.addEventListener('mousedown', function (e) { e.preventDefault(); });  // keep field focus
    pop.addEventListener('click', onPopClick);
    document.body.appendChild(pop);
    return pop;
  }

  /* Put the popup under the field; flip above it when the bottom of the window
     is too close, and never let it hang off the left or right edge. */
  function place(anchor, box) {
    var r = anchor.getBoundingClientRect();
    var w = box.offsetWidth, h = box.offsetHeight;
    var top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) {
      top = (r.top - 6 - h >= 8) ? r.top - 6 - h : Math.max(8, window.innerHeight - h - 8);
    }
    var left = r.left;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
    if (left < 8) left = 8;
    box.style.top = Math.round(top) + 'px';
    box.style.left = Math.round(left) + 'px';
  }

  function renderDate() {
    var box = popup(), sel = parseVal(host.value), tk = dayKey(today()), html;

    if (page === 'years') {
      var y0 = view.y - 6, cells = '';
      for (var i = 0; i < 16; i++) {
        var yy = y0 + i, ok = allowed(host, String(yy), 4);
        cells += '<button type="button" data-bkyear="' + yy + '"' + (ok ? '' : ' disabled') +
                 ' class="' + (yy === view.y ? 'is-selected' : '') + '">' + yy + '</button>';
      }
      html = head(view.y, 'year') + '<div class="bkdp-list is-years">' + cells + '</div>';

    } else if (page === 'months' || mode === 'month') {
      var mc = '';
      for (var k = 0; k < 12; k++) {
        var key = monKey(view.y, k), on = (mode === 'month' && sel) ? (sel.y === view.y && sel.m === k) : (k === view.m);
        mc += '<button type="button" data-bkmonth="' + k + '"' + (allowed(host, key, 7) ? '' : ' disabled') +
              ' class="' + (on ? 'is-selected' : '') + '">' + MON3[k] + '</button>';
      }
      html = head(view.y, 'month') + '<div class="bkdp-list">' + mc + '</div>';

    } else {
      var first = new Date(view.y, view.m, 1);
      var start = new Date(view.y, view.m, 1 - first.getDay());
      var selKey = sel ? dayKey(new Date(sel.y, sel.m, sel.d)) : '';
      var g = '';
      for (var n = 0; n < 42; n++) {
        var d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + n);
        var kk = dayKey(d), cls = [];
        if (d.getMonth() !== view.m) cls.push('is-out');
        if (kk === tk) cls.push('is-today');
        if (kk === selKey) cls.push('is-selected');
        g += '<button type="button" data-bkday="' + kk + '" class="' + cls.join(' ') + '"' +
             (allowed(host, kk, 10) ? '' : ' disabled') + '>' + d.getDate() + '</button>';
      }
      html = head(MONTHS[view.m] + ' ' + view.y, 'days') +
             '<div class="bkdp-weekdays">' + DOW.map(function (x) { return '<span>' + x + '</span>'; }).join('') + '</div>' +
             '<div class="bkdp-grid">' + g + '</div>';
    }

    html += '<div class="bkdp-foot">' +
            '<button type="button" data-bk="clear">Clear</button>' +
            '<button type="button" data-bk="today">' + (mode === 'month' ? 'This month' : 'Today') + '</button>' +
            '</div>';
    box.innerHTML = html;
    place(host, box);
  }

  function head(title, kind) {
    var caret = (kind === 'days' || kind === 'month') ? '<span class="bk-caret">&#9662;</span>' : '';
    return '<div class="bkdp-head">' +
             '<button type="button" class="bkdp-title" data-bk="title">' + esc(title) + caret + '</button>' +
             '<div class="bkdp-nav">' +
               '<button type="button" data-bk="prev" title="Previous">&#8249;</button>' +
               '<button type="button" data-bk="next" title="Next">&#8250;</button>' +
             '</div></div>';
  }

  function commit(value) {
    if (!host) return;
    host.value = value;
    fire(host);
    close();
  }

  function onPopClick(e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b || b.disabled || !host) return;
    var what = b.getAttribute('data-bk');

    if (b.hasAttribute('data-bkday')) return commit(b.getAttribute('data-bkday'));
    if (b.hasAttribute('data-bkmonth')) {
      var m = +b.getAttribute('data-bkmonth');
      if (mode === 'month') return commit(monKey(view.y, m));
      view.m = m; page = 'days'; return renderDate();
    }
    if (b.hasAttribute('data-bkyear')) {
      view.y = +b.getAttribute('data-bkyear');
      page = (mode === 'month') ? 'months' : 'months';
      return renderDate();
    }
    if (what === 'title') {
      page = (page === 'days') ? 'months' : 'years';
      return renderDate();
    }
    if (what === 'prev' || what === 'next') {
      var step = (what === 'prev') ? -1 : 1;
      if (page === 'years') view.y += step * 16;
      else if (page === 'months' || mode === 'month') view.y += step;
      else {
        var d2 = new Date(view.y, view.m + step, 1);
        view.y = d2.getFullYear(); view.m = d2.getMonth();
      }
      return renderDate();
    }
    if (what === 'clear') return commit('');
    if (what === 'today') {
      var t = today();
      return commit(mode === 'month' ? monKey(t.getFullYear(), t.getMonth()) : dayKey(t));
    }
  }

  function openDate(input) {
    if (input.disabled || input.readOnly) return;
    closeSel();
    host = input;
    mode = (input.type === 'month') ? 'month' : 'date';
    page = (mode === 'month') ? 'months' : 'days';
    var v = parseVal(input.value) || parseVal(limits(input).max) || null;
    var t = today();
    view = v ? { y: v.y, m: v.m } : { y: t.getFullYear(), m: t.getMonth() };
    var box = popup();
    box.classList.add('is-open');
    renderDate();
  }

  function close() {
    if (pop) pop.classList.remove('is-open');
    host = null;
  }

  // ── the dropdown list that replaces the browser's <select> list ───────────
  var selPop = null, selHost = null;

  function selPopup() {
    if (selPop) return selPop;
    selPop = document.createElement('div');
    selPop.className = 'bksel';
    selPop.setAttribute('role', 'listbox');
    selPop.addEventListener('mousedown', function (e) { e.preventDefault(); });
    selPop.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('.bksel-opt') : null;
      if (!b || b.disabled || !selHost) return;
      var i = +b.getAttribute('data-bki');
      if (selHost.selectedIndex !== i) { selHost.selectedIndex = i; fire(selHost); }
      closeSel();
    });
    document.body.appendChild(selPop);
    return selPop;
  }

  /* Options are read fresh every time it opens - several pages rebuild their
     <option> list with innerHTML while the page is running. */
  function openSel(sel) {
    if (sel.disabled || sel.multiple || sel.size > 1) return;
    close();
    selHost = sel;
    var box = selPopup(), html = '', any = false;

    Array.prototype.forEach.call(sel.children, function (child) {
      if (child.tagName === 'OPTGROUP') {
        html += '<div class="bksel-group">' + esc(child.label) + '</div>';
        Array.prototype.forEach.call(child.children, function (o) { html += optHtml(o, sel); any = true; });
      } else if (child.tagName === 'OPTION') {
        html += optHtml(child, sel); any = true;
      }
    });
    box.innerHTML = any ? html : '<div class="bksel-empty">Nothing to choose</div>';

    var r = sel.getBoundingClientRect();
    box.style.width = Math.max(r.width, 170) + 'px';
    box.classList.add('is-open');
    box.scrollTop = 0;
    place(sel, box);
    var on = box.querySelector('.bksel-opt.is-selected');
    if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest' });
  }

  function optHtml(o, sel) {
    var i = Array.prototype.indexOf.call(sel.options, o);
    var txt = o.label || o.text || o.value || ' ';
    return '<button type="button" class="bksel-opt' + (o.selected ? ' is-selected' : '') + '"' +
           ' data-bki="' + i + '"' + (o.disabled ? ' disabled' : '') + ' role="option"' +
           ' title="' + esc(txt) + '">' +
           '<span class="bk-lbl">' + esc(txt) + '</span><span class="bk-tick">&#10003;</span></button>';
  }

  function closeSel() {
    if (selPop) selPop.classList.remove('is-open');
    selHost = null;
  }

  // ── wiring ────────────────────────────────────────────────────────────────
  function wireDate(input) {
    if (input.__bkdp || input.getAttribute('data-bkdp') === 'off') return;
    input.__bkdp = 1;
    input.classList.add('bkdp-on');
    // mousedown, not click: it beats the browser to its own popup.
    input.addEventListener('mousedown', function (e) {
      if (input.disabled || input.readOnly) return;
      e.preventDefault();
      input.focus();
      (host === input) ? close() : openDate(input);
    });
    // Space / Enter / Down open the browser's picker on some builds - use ours.
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(); return; }
      if ((e.key === ' ' || e.key === 'Enter' || e.key === 'ArrowDown') && !e.altKey) {
        e.preventDefault(); openDate(input);
      }
    });
  }

  function wireSelect(sel) {
    if (sel.__bkdp || sel.getAttribute('data-bkdp') === 'off') return;
    if (sel.multiple || sel.size > 1) return;          // list boxes keep their own look
    sel.__bkdp = 1;
    sel.classList.add('bksel-on');
    sel.addEventListener('mousedown', function (e) {
      if (sel.disabled) return;
      e.preventDefault();                               // stops the browser's grey list
      sel.focus();
      (selHost === sel) ? closeSel() : openSel(sel);
    });
    sel.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeSel(); });
    sel.addEventListener('blur', closeSel);
  }

  function scan(root) {
    if (!root || root.nodeType !== 1) return;
    if (root.matches) {
      if (root.matches('input[type="date"],input[type="month"]')) wireDate(root);
      else if (root.matches('select')) wireSelect(root);
    }
    if (!root.querySelectorAll) return;
    Array.prototype.forEach.call(root.querySelectorAll('input[type="date"],input[type="month"]'), wireDate);
    Array.prototype.forEach.call(root.querySelectorAll('select'), wireSelect);
  }

  function start() {
    scan(document.body);
    // Several pages build their filters after loading data, so watch for new ones.
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes;
        for (var j = 0; j < added.length; j++) scan(added[j]);
      }
    }).observe(document.documentElement, { childList: true, subtree: true });

    document.addEventListener('mousedown', function (e) {
      if (pop && pop.contains(e.target)) return;
      if (selPop && selPop.contains(e.target)) return;
      if (e.target === host || e.target === selHost) return;
      close(); closeSel();
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { close(); closeSel(); } });
    // Follow the field if the page scrolls; give up if the window is resized.
    window.addEventListener('scroll', function () {
      if (host && pop) place(host, pop);
      if (selHost && selPop) place(selHost, selPop);
    }, true);
    window.addEventListener('resize', function () { close(); closeSel(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
