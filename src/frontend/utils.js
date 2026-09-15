// 通用工具函数

function showToast(message, duration = 2000) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = message;
  document.body.appendChild(toast);

  setTimeout(() => toast.remove(), duration);
}

function getStatusTag(status) {
  const map = {
    '匹配': '<span class="tag tag-match">匹配</span>',
    '部分匹配': '<span class="tag tag-inferred">部分匹配</span>',
    '不满足': '<span class="tag tag-mismatch">不满足</span>',
    '信息不足': '<span class="tag tag-unknown">信息不足</span>',
    '信息冲突': '<span class="tag tag-pending">信息冲突</span>',
    '推断': '<span class="tag tag-inferred">有限推断</span>'
  };
  return map[status] || `<span class="tag tag-unknown">${status}</span>`;
}

function getLayerTag(layer) {
  const map = {
    'recommend': '<span class="tag tag-recommend">推荐</span>',
    'pending': '<span class="tag tag-pending">待定</span>',
    'reject': '<span class="tag tag-reject">不推荐</span>'
  };
  return map[layer] || layer;
}

function getLayerName(layer) {
  const map = {
    'recommend': '推荐',
    'pending': '待定',
    'reject': '不推荐'
  };
  return map[layer] || layer;
}

function getConfidenceBar(value) {
  const percent = Math.round(value * 100);
  let color = 'var(--success)';
  if (percent < 60) color = 'var(--danger)';
  else if (percent < 80) color = 'var(--warning)';

  return `<div style="display:flex;align-items:center;gap:8px">
    <div style="flex:1;height:6px;background:#eee;border-radius:3px;overflow:hidden">
      <div style="width:${percent}%;height:100%;background:${color};border-radius:3px"></div>
    </div>
    <span style="font-size:12px;color:var(--muted);min-width:36px">${percent}%</span>
  </div>`;
}

function truncateText(text, maxLen = 50) {
  if (!text) return '';
  return text.length > maxLen ? text.substring(0, maxLen) + '...' : text;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[char]));
}

function formatErrorDetails(details) {
  if (!details || typeof details !== 'object') return '';
  if (Array.isArray(details.issues) && details.issues.length) {
    return `详情：${details.issues.join('；')}`;
  }
  if (details.reason) return `原因：${details.reason}`;
  if (Array.isArray(details.rows) && details.rows.length) {
    return `涉及 CSV 行：${details.rows.join('、')}`;
  }
  return '';
}

function formatFailure(item) {
  const code = item && item.code ? `[${item.code}] ` : '';
  const message = item && item.message ? item.message : '未知错误';
  const diagnostic = formatErrorDetails(item && item.details);
  return `${item && item.resume_id ? item.resume_id : '未知候选人'}：${code}${message}${diagnostic ? `；${diagnostic}` : ''}`;
}

function highlightEvidence(text, keywords) {
  if (!text || !keywords) return text;
  let result = text;
  keywords.forEach(kw => {
    const regex = new RegExp(`(${kw})`, 'gi');
    result = result.replace(regex, '<mark>$1</mark>');
  });
  return result;
}
