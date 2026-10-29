/**
 * Shared batch operations for account + briefing views.
 * Depends on: window.showErrorToast, window.openInBackground (from base.html)
 */
(function() {
  // --- Batch selection mode ---
  var selectionMode = false;
  var anchorIndex = -1;

  function getBatchBar() { return document.getElementById('batch-bar'); }
  function getBatchCount() { return document.getElementById('batch-count'); }

  function getSelectedIds() {
    return Array.from(document.querySelectorAll('.batch-select-cb:checked')).map(function(cb) { return cb.dataset.taskId; });
  }

  function updateBatchCount() {
    var ids = getSelectedIds();
    var countEl = getBatchCount();
    if (countEl) countEl.textContent = ids.length + ' selected';
    var bar = getBatchBar();
    if (bar) {
      if (ids.length > 0) bar.classList.remove('hidden');
      else bar.classList.add('hidden');
    }
  }

  function enterSelectionMode() {
    selectionMode = true;
    document.querySelectorAll('.batch-checkbox').forEach(function(el) { el.classList.remove('hidden'); });
    updateBatchCount();
  }

  window.exitSelectionMode = function() {
    selectionMode = false;
    anchorIndex = -1;
    document.querySelectorAll('.batch-checkbox').forEach(function(el) { el.classList.add('hidden'); });
    document.querySelectorAll('.batch-select-cb').forEach(function(cb) { cb.checked = false; });
    var bar = getBatchBar();
    if (bar) bar.classList.add('hidden');
    var dd = document.getElementById('batch-snooze-dropdown');
    if (dd) dd.classList.add('hidden');
  };

  window.enterSelectionMode = enterSelectionMode;
  window.isSelectionMode = function() { return selectionMode; };
  // Delegated click handler for batch checkboxes — survives HTMX swaps,
  // no per-element inline handler needed.
  document.addEventListener('click', function(e) {
    var cb = e.target.closest('.batch-select-cb');
    if (!cb) return;
    // Prevent card hx-get from firing (belt-and-suspenders with hx-trigger filter)
    e.stopPropagation();
    if (e.shiftKey && anchorIndex >= 0) {
      // Range select from anchor to clicked checkbox
      e.preventDefault(); // Prevent text selection + default checkbox toggle
      var cards = Array.from(document.querySelectorAll('#task-list > [data-task-id]'));
      var clickedCard = cb.closest('div[data-task-id]');
      if (clickedCard) {
        var clickedIdx = cards.indexOf(clickedCard);
        if (clickedIdx >= 0) {
          var lo = Math.min(anchorIndex, clickedIdx);
          var hi = Math.max(anchorIndex, clickedIdx);
          for (var i = lo; i <= hi; i++) {
            var c = cards[i].querySelector('.batch-select-cb');
            if (c) c.checked = true;
          }
        }
      }
    } else {
      // Single click — browser already toggled checkbox. Update anchor.
      var cards = Array.from(document.querySelectorAll('#task-list > [data-task-id]'));
      var clickedCard = cb.closest('div[data-task-id]');
      if (clickedCard) {
        var idx = cards.indexOf(clickedCard);
        if (idx >= 0) anchorIndex = idx;
      }
    }
    updateBatchCount();
  });

  window.resetBatchAnchor = function() { anchorIndex = -1; };

  // --- Batch action: get current task list URL for HTMX refresh ---
  function getRefreshUrl() {
    // Account view: /accounts/{id}/tasks?tier=...&filters=...
    // Briefing view: /briefing/tasks?filters=...
    if (window._batchRefreshUrl) return window._batchRefreshUrl();
    return null;
  }

  function refreshAfterBatch() {
    var url = getRefreshUrl();
    if (url) {
      window.exitSelectionMode();
      htmx.ajax('GET', url, {target: '#task-list', swap: 'innerHTML'});
    } else {
      window.location.reload();
    }
  }

  window.batchOpenInBC = function() {
    var ids = getSelectedIds();
    ids.forEach(function(id) {
      var card = document.getElementById('task-card-' + id);
      if (card && card.dataset.taskUrl) window.openInBackground(card.dataset.taskUrl);
    });
  };

  window.batchStar = function(starred) {
    var ids = getSelectedIds();
    if (ids.length === 0) return;
    fetch('/api/tasks/batch-star', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: ids, starred: starred })
    }).then(function(r) { if (r.ok) refreshAfterBatch(); else showErrorToast('Batch star failed (' + r.status + ')'); })
      .catch(function() { showErrorToast('Batch star failed (network error)'); });
  };

  window.batchVote = function(value) {
    var ids = getSelectedIds();
    if (ids.length === 0) return;
    fetch('/api/tasks/batch-vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: ids, value: value })
    }).then(function(r) { if (r.ok) refreshAfterBatch(); else showErrorToast('Batch vote failed (' + r.status + ')'); })
      .catch(function() { showErrorToast('Batch vote failed (network error)'); });
  };

  window.batchDismiss = function() {
    var ids = getSelectedIds();
    if (ids.length === 0) return;
    if (!confirm('Dismiss ' + ids.length + ' task(s)? This will also register a downvote for each.')) return;
    fetch('/api/tasks/batch-dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: ids })
    }).then(function(r) { if (r.ok) refreshAfterBatch(); else showErrorToast('Batch dismiss failed (' + r.status + ')'); })
      .catch(function() { showErrorToast('Batch dismiss failed (network error)'); });
  };

  window.batchSnooze = function(preset) {
    var ids = getSelectedIds();
    if (ids.length === 0) return;
    var until = window.computeSnoozeUntil(preset);
    if (!until) return;
    fetch('/api/tasks/batch-snooze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_ids: ids, until: until })
    }).then(function(r) { if (r.ok) refreshAfterBatch(); else showErrorToast('Batch snooze failed (' + r.status + ')'); })
      .catch(function() { showErrorToast('Batch snooze failed (network error)'); });
  };

  window.toggleBatchSnooze = function() {
    var dd = document.getElementById('batch-snooze-dropdown');
    if (dd) dd.classList.toggle('hidden');
  };

  // Close batch snooze dropdown on outside click
  document.addEventListener('click', function(e) {
    var wrapper = document.getElementById('batch-snooze-wrapper');
    var dd = document.getElementById('batch-snooze-dropdown');
    if (wrapper && dd && !wrapper.contains(e.target)) {
      dd.classList.add('hidden');
    }
  });

  // --- Keyboard: x / Shift+x for batch toggle / range select ---
  window.batchKeyHandler = function(e, selectedIndex) {
    if (e.key !== 'x' && e.key !== 'X') return false;
    e.preventDefault();
    if (!selectionMode) {
      enterSelectionMode();
      anchorIndex = selectedIndex >= 0 ? selectedIndex : -1;
    } else {
      var cards = Array.from(document.querySelectorAll('#task-list > [data-task-id]'));
      if (selectedIndex >= 0 && selectedIndex < cards.length) {
        if (e.shiftKey && anchorIndex >= 0) {
          // Range select: check all cards between anchor and current
          var lo = Math.min(anchorIndex, selectedIndex);
          var hi = Math.max(anchorIndex, selectedIndex);
          for (var i = lo; i <= hi; i++) {
            var cb = cards[i].querySelector('.batch-select-cb');
            if (cb) cb.checked = true;
          }
          // Don't update anchor — allows extending from same start
        } else {
          // Single toggle + update anchor
          var cb = cards[selectedIndex].querySelector('.batch-select-cb');
          if (cb) cb.checked = !cb.checked;
          anchorIndex = selectedIndex;
        }
        updateBatchCount();
      }
    }
    return true;
  };
})();
