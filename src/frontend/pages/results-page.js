(function (global) {
  let currentTab = 'recommend';
  let jobId = '';

  function updateCounts(summary, failures) {
    document.getElementById('totalNum').textContent = summary.result_total;
    document.getElementById('recommendNum').textContent = summary.recommend;
    document.getElementById('pendingNum').textContent = summary.pending;
    document.getElementById('rejectNum').textContent = summary.reject;
    document.getElementById('recommendBadge').textContent = summary.recommend;
    document.getElementById('pendingBadge').textContent = summary.pending;
    document.getElementById('rejectBadge').textContent = summary.reject;

    document.getElementById('limitNotice').textContent =
      `导入 ${summary.source_total} 份；本轮选择 ${summary.selected_total} 份；` +
      `已处理 ${summary.processed} 份 = 成功 ${summary.succeeded} 份 + 失败 ${summary.failed} 份；` +
      `剩余 ${summary.remaining} 份；最终结果 ${summary.result_total} 份。`;

    const failureNotice = document.getElementById('failureNotice');
    if (failures && failures.length > 0) {
      failureNotice.classList.remove('hidden');
      failureNotice.innerHTML = '<strong>分析失败明细</strong><br>' + failures.map(item =>
        escapeHtml(formatFailure(item))
      ).join('<br>');
    } else {
      failureNotice.classList.add('hidden');
      failureNotice.textContent = '';
    }
  }

  function renderTable(items, layer) {
    const tbody = document.getElementById('resultsBody');
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">暂无${getLayerName(layer)}候选人</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map(item => `
      <tr data-resume-id="${escapeHtml(item.resume_id)}">
        <td><strong>${escapeHtml(item.resume_id)}</strong></td>
        <td>${escapeHtml(item.resume_excerpt || '未提供')}</td>
        <td>${getLayerTag(item.ai_layer)}</td>
        <td>${item.review_status === 'reviewed' ? getLayerTag(item.final_layer) : '<span class="tag tag-unknown">未复核</span>'}</td>
        <td>${item.review_status === 'reviewed'
          ? `<span style="display:inline-block;max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)" title="${escapeHtml(item.review_reason)}">${escapeHtml(truncateText(item.review_reason, 28))}</span>`
          : '<span style="color:var(--muted)">—</span>'}</td>
        <td>${item.risk ? '<span class="tag tag-reject">有风险</span>' : '<span class="tag tag-match">无</span>'}</td>
        <td style="min-width:100px">${getConfidenceBar(item.match_score / 100)}</td>
        <td style="min-width:100px">${getConfidenceBar(item.evidence_confidence)}</td>
        <td><button class="btn btn-outline btn-sm view-detail-btn" data-resume-id="${escapeHtml(item.resume_id)}">查看</button></td>
      </tr>
    `).join('');

    tbody.querySelectorAll('tr[data-resume-id]').forEach(row => {
      row.addEventListener('click', () => viewDetail(row.dataset.resumeId));
    });
    tbody.querySelectorAll('.view-detail-btn').forEach(button => {
      button.addEventListener('click', event => {
        event.stopPropagation();
        viewDetail(button.dataset.resumeId);
      });
    });
  }

  async function switchTab(tab, button) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    await loadResults(tab);
  }

  function viewDetail(resumeId) {
    ScreeningState.saveCurrentResumeId(resumeId);
    global.location.href = `detail.html?id=${encodeURIComponent(resumeId)}`;
  }

  async function loadResults(layer) {
    const tbody = document.getElementById('resultsBody');
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">正在从后端读取结果...</td></tr>';
    try {
      const response = await ScreeningApi.getResults(jobId, layer);
      updateCounts(response.summary, response.failures);
      renderTable(response.items, layer);
    } catch (error) {
      ScreeningFeedback.showApiError(error, '加载筛选结果失败：');
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--danger);padding:40px">结果加载失败，请确认分析已经完成。</td></tr>';
    }
  }

  async function initializePage() {
    jobId = ScreeningState.getCurrentJobId();
    if (!jobId) {
      document.getElementById('resultsBody').innerHTML =
        '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">请先创建筛选任务。</td></tr>';
      return;
    }
    await loadResults(currentTab);
  }

  document.querySelectorAll('.tab').forEach(button => {
    button.addEventListener('click', () => switchTab(button.dataset.tab, button));
  });
  document.getElementById('backToCriteriaBtn').addEventListener('click', () => {
    global.location.href = 'criteria.html';
  });
  initializePage();
})(window);
