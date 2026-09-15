(function (global) {
  const uploadArea = document.getElementById('uploadArea');
  const fileInput = document.getElementById('fileInput');
  let importPreview = null;
  let createdJobId = null;

  function resetPanels() {
    ['roleSelectCard', 'previewCard', 'jobInfoCard', 'actionBar'].forEach(id => {
      document.getElementById(id).classList.add('hidden');
    });
  }

  async function handleFile(file) {
    if (!file.name.toLowerCase().endsWith('.csv')) {
      showToast('请上传 CSV 文件');
      return;
    }
    createdJobId = null;
    importPreview = null;
    ScreeningState.clearCurrentNavigation();
    resetPanels();
    showToast('正在由后端预检 CSV...');
    try {
      importPreview = await ScreeningApi.previewImport(file);
      renderRoleOptions(importPreview);
      showToast('CSV 预检完成，请选择岗位');
    } catch (error) {
      ScreeningFeedback.showApiError(error, 'CSV 预检失败：');
    }
  }

  function renderRoleOptions(preview) {
    const select = document.getElementById('roleSelect');
    select.innerHTML = '<option value="" selected disabled>-- 请选择一个具体岗位 --</option>' +
      preview.roles.map(role =>
        `<option value="${escapeHtml(role.name)}">${escapeHtml(role.name)} (${role.count} 条)</option>`
      ).join('');
    document.getElementById('roleStats').innerHTML = '各岗位数量: ' + preview.roles.map(role =>
      `<strong>${escapeHtml(role.name)}</strong>: ${role.count}`
    ).join(' | ');
    document.getElementById('roleSelectCard').classList.remove('hidden');
  }

  function selectedRolePreview() {
    const selectedRole = document.getElementById('roleSelect').value;
    return importPreview && importPreview.roles.find(role => role.name === selectedRole);
  }

  function onRoleChange() {
    createdJobId = null;
    const role = selectedRolePreview();
    if (!role) {
      resetPanels();
      return;
    }

    document.getElementById('previewCard').classList.remove('hidden');
    document.getElementById('fileInfo').textContent =
      `文件: ${importPreview.file_name} | 岗位: ${role.name} | 共 ${role.count} 条记录`;
    const previewFields = ['resume_id', 'resume_text', 'job_role', 'job_description'];
    document.querySelector('#previewTable thead').innerHTML = '<tr>' +
      previewFields.map(field => `<th>${escapeHtml(field)}</th>`).join('') + '</tr>';
    document.querySelector('#previewTable tbody').innerHTML = role.preview_rows.map(row =>
      '<tr>' + previewFields.map(field =>
        `<td>${escapeHtml(truncateText(row[field] || '', 40))}</td>`
      ).join('') + '</tr>'
    ).join('');

    if (!role.jd_valid) {
      document.getElementById('jobInfoCard').classList.add('hidden');
      document.getElementById('actionBar').classList.add('hidden');
      showToast(role.issues.join('；'), 6000);
      return;
    }
    document.getElementById('jobRole').value = role.name;
    document.getElementById('jobDesc').value = role.jd_text || '';
    document.getElementById('jobInfoCard').classList.remove('hidden');
    document.getElementById('actionBar').classList.remove('hidden');
  }

  async function startAnalysis() {
    const role = selectedRolePreview();
    if (!role || !importPreview) {
      showToast('请先上传 CSV 并选择一个具体岗位');
      return;
    }
    const candidateLimit = Number(document.getElementById('candidateLimit').value);
    if (!Number.isInteger(candidateLimit) || candidateLimit < 1 || candidateLimit > 20) {
      showToast('本轮分析数量必须是 1 到 20 的整数');
      return;
    }

    const button = document.getElementById('createTaskBtn');
    ScreeningFeedback.setButtonLoading(button, true, createdJobId ? '重新提取条件...' : '正在创建任务...');
    try {
      if (!createdJobId) {
        const created = await ScreeningApi.createJobFromPreview(
          importPreview.preview_id,
          role.name,
          candidateLimit
        );
        createdJobId = created.job_id;
        ScreeningState.saveCurrentJobId(createdJobId);
        showToast(`任务已创建，已导入 ${created.import_summary.valid} 份有效简历`);
        ScreeningFeedback.setButtonLoading(button, true, '分析引擎正在提取条件...');
      }
      await ScreeningApi.extractCriteria(createdJobId);
      showToast('筛选条件已提取，请由 HR 确认');
      global.location.href = 'criteria.html';
    } catch (error) {
      ScreeningFeedback.showApiError(error, '创建任务失败：');
      ScreeningFeedback.setButtonLoading(button, false);
    }
  }

  uploadArea.addEventListener('click', () => fileInput.click());
  uploadArea.addEventListener('dragover', event => {
    event.preventDefault();
    uploadArea.style.borderColor = 'var(--primary)';
  });
  uploadArea.addEventListener('dragleave', () => {
    uploadArea.style.borderColor = 'var(--line)';
  });
  uploadArea.addEventListener('drop', event => {
    event.preventDefault();
    uploadArea.style.borderColor = 'var(--line)';
    if (event.dataTransfer.files.length) handleFile(event.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', event => {
    if (event.target.files.length) handleFile(event.target.files[0]);
  });

  document.getElementById('roleSelect').addEventListener('change', onRoleChange);
  document.getElementById('createTaskBtn').addEventListener('click', startAnalysis);
})(window);
