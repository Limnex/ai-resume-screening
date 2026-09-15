(function (global) {
  let criteria = [];
  let nextId = 1;
  let jobId = '';
  let confirmedVersion = null;
  let analysisIdempotencyKey = null;

  function renderJobSummary(response) {
    const job = response.job;
    document.getElementById('jobSummary').innerHTML =
      `<strong>${escapeHtml(job.job_role)}</strong> | ` +
      `有效简历: ${response.import_summary.valid} 份 | ` +
      `本轮计划分析: ${escapeHtml(job.candidate_limit)} 份` +
      `<div class="evidence-block" style="margin-top:12px;white-space:pre-wrap">${escapeHtml(job.jd_text)}</div>`;
  }

  function renderTable() {
    const tbody = document.getElementById('criteriaBody');
    if (criteria.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px">暂无筛选条件</td></tr>';
      return;
    }
    tbody.innerHTML = criteria.map((criterion, index) => `
      <tr>
        <td>${index + 1}</td>
        <td><textarea rows="2" class="criterion-name criteria-table-control" data-criterion-id="${criterion.id}">${escapeHtml(criterion.name)}</textarea></td>
        <td><textarea rows="2" class="criterion-requirement criteria-table-control" data-criterion-id="${criterion.id}">${escapeHtml(criterion.requirement || criterion.source_evidence || '')}</textarea></td>
        <td>
          <select class="criterion-category criteria-table-control" data-criterion-id="${criterion.id}">
            ${[
              ['experience', '经验'], ['skill', '技能'], ['responsibility', '职责'],
              ['domain', '领域'], ['certification', '证书'], ['other', '其他']
            ].map(([value, label]) => `<option value="${value}" ${criterion.category === value ? 'selected' : ''}>${label}</option>`).join('')}
          </select>
        </td>
        <td>
          <select class="criterion-type criteria-table-control" data-criterion-id="${criterion.id}">
            <option value="必要条件" ${criterion.type === '必要条件' ? 'selected' : ''}>必要条件</option>
            <option value="加分条件" ${criterion.type === '加分条件' ? 'selected' : ''}>加分条件</option>
          </select>
        </td>
        <td class="criteria-source" title="${escapeHtml(criterion.source_evidence || '')}">${escapeHtml(criterion.source || 'HR 新增')}${criterion.source_evidence_valid === false ? '（证据待核）' : ''}</td>
        <td><button class="btn btn-outline btn-sm remove-condition-btn" data-criterion-id="${criterion.id}" style="color:var(--danger);border-color:var(--danger)">删除</button></td>
      </tr>
    `).join('');
  }

  function findCriterion(id) {
    return criteria.find(criterion => criterion.id === id);
  }

  function markCriteriaDirty() {
    confirmedVersion = null;
    analysisIdempotencyKey = null;
  }

  function updateName(id, value) {
    const item = findCriterion(id);
    if (item) item.name = value;
    markCriteriaDirty();
  }

  function updateType(id, value) {
    const item = findCriterion(id);
    if (item) item.type = value;
    markCriteriaDirty();
  }

  function updateRequirement(id, value) {
    const item = findCriterion(id);
    if (item) item.requirement = value;
    markCriteriaDirty();
  }

  function updateCategory(id, value) {
    const item = findCriterion(id);
    if (item) item.category = value;
    markCriteriaDirty();
  }

  function removeCondition(id) {
    criteria = criteria.filter(criterion => criterion.id !== id);
    markCriteriaDirty();
    renderTable();
    showToast('条件已删除');
  }

  function addCondition() {
    criteria.push({
      id: nextId++,
      name: '',
      category: 'other',
      requirement: '',
      minimum_value: null,
      type: '加分条件',
      source: 'HR 新增'
    });
    markCriteriaDirty();
    renderTable();
  }

  function renderAnalysisStatus(status) {
    const percent = Math.round((status.progress || 0) * 100);
    document.getElementById('analysisStatusCard').classList.remove('hidden');
    document.getElementById('analysisStatusText').textContent =
      `导入 ${status.source_total} 份，本轮选择 ${status.selected_total} 份；` +
      `已处理 ${status.processed} 份，成功 ${status.succeeded} 份，失败 ${status.failed} 份，剩余 ${status.remaining} 份。`;
    const failureBox = document.getElementById('analysisFailures');
    if (status.failures && status.failures.length > 0) {
      failureBox.classList.remove('hidden');
      failureBox.textContent = '失败明细：' + status.failures.map(formatFailure).join('；');
    } else if (status.fatal_error) {
      failureBox.classList.remove('hidden');
      failureBox.textContent = '任务失败：' + formatFailure({ resume_id: '整批任务', ...status.fatal_error });
    } else {
      failureBox.classList.add('hidden');
      failureBox.textContent = '';
    }
    document.getElementById('analysisProgressBar').style.width = `${percent}%`;
  }

  async function waitForAnalysis() {
    for (let attempt = 0; attempt < 300; attempt++) {
      const status = await ScreeningApi.getAnalysisStatus(jobId);
      renderAnalysisStatus(status);
      if (status.status !== 'analyzing') {
        if (status.status === 'completed' || status.status === 'completed_with_errors') return status;
        throw new ApiError('ANALYSIS_FAILED', `分析未完成，当前状态：${status.status}`);
      }
      await new Promise(resolve => setTimeout(resolve, 2000));
    }
    throw new ApiError('ANALYSIS_TIMEOUT', '等待分析超过10分钟，请稍后从结果页重试。');
  }

  async function confirmCriteria() {
    const valid = criteria.filter(criterion => criterion.name.trim());
    if (valid.length === 0) {
      showToast('请至少保留一个筛选条件');
      return;
    }
    if (valid.some(criterion => !(criterion.requirement || criterion.source_evidence || '').trim())) {
      showToast('请为每个条件填写具体要求');
      return;
    }
    const button = document.getElementById('confirmBtn');
    ScreeningFeedback.setButtonLoading(button, true, confirmedVersion ? '正在重新连接分析...' : '正在保存标准...');
    try {
      if (!confirmedVersion) {
        const confirmed = await ScreeningApi.confirmCriteria(jobId, valid.map(item => ({
          id: item.id,
          name: item.name.trim(),
          category: item.category,
          type: item.type,
          requirement: (item.requirement || item.source_evidence || '').trim(),
          minimum_value: item.minimum_value ?? null
        })));
        confirmedVersion = confirmed.criteria_version;
        ScreeningState.saveCurrentCriteriaVersion(confirmedVersion);
        analysisIdempotencyKey = `analysis-${jobId}-criteria-${confirmedVersion}`;
      }

      ScreeningFeedback.setButtonLoading(button, true, '正在启动分析引擎...');
      const started = await ScreeningApi.startAnalysis(jobId, confirmedVersion, analysisIdempotencyKey);
      renderAnalysisStatus({
        ...started,
        processed: 0,
        succeeded: 0,
        failed: 0,
        remaining: started.selected_total,
        progress: 0
      });
      ScreeningFeedback.setButtonLoading(button, true, '分析引擎处理中...');
      await waitForAnalysis();
      showToast('分析完成，正在打开结果页');
      global.location.href = 'results.html';
    } catch (error) {
      ScreeningFeedback.showApiError(error, '分析启动失败：');
      ScreeningFeedback.setButtonLoading(button, false);
    }
  }

  async function initializePage() {
    jobId = ScreeningState.getCurrentJobId();
    if (!jobId) {
      document.getElementById('jobSummary').textContent = '请先在创建任务页上传 CSV 并选择岗位。';
      document.getElementById('confirmBtn').disabled = true;
      renderTable();
      return;
    }
    document.getElementById('criteriaBody').innerHTML =
      '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:30px">正在从后端读取筛选条件...</td></tr>';
    try {
      const [job, criteriaResponse] = await Promise.all([
        ScreeningApi.getJob(jobId),
        ScreeningApi.getCriteria(jobId)
      ]);
      renderJobSummary(job);
      criteria = criteriaResponse.criteria || [];
      nextId = Math.max(0, ...criteria.map(item => Number(item.id) || 0)) + 1;
      renderTable();
    } catch (error) {
      ScreeningFeedback.showApiError(error, '加载筛选条件失败：');
      document.getElementById('criteriaBody').innerHTML =
        '<tr><td colspan="7" style="text-align:center;color:var(--danger);padding:30px">筛选条件加载失败，请返回创建任务页重试。</td></tr>';
      document.getElementById('confirmBtn').disabled = true;
    }
  }

  const criteriaBody = document.getElementById('criteriaBody');
  criteriaBody.addEventListener('change', event => {
    const target = event.target;
    const id = Number(target.dataset.criterionId);
    if (!id) return;
    if (target.classList.contains('criterion-name')) updateName(id, target.value);
    if (target.classList.contains('criterion-requirement')) updateRequirement(id, target.value);
    if (target.classList.contains('criterion-category')) updateCategory(id, target.value);
    if (target.classList.contains('criterion-type')) updateType(id, target.value);
  });
  criteriaBody.addEventListener('click', event => {
    const button = event.target.closest('.remove-condition-btn');
    if (button) removeCondition(Number(button.dataset.criterionId));
  });
  document.getElementById('addConditionBtn').addEventListener('click', addCondition);
  document.getElementById('backToHomeBtn').addEventListener('click', () => {
    global.location.href = 'index.html';
  });
  document.getElementById('confirmBtn').addEventListener('click', confirmCriteria);
  initializePage();
})(window);
