(function (global) {
  let detail = null;
  let jobId = '';
  let resumeId = '';
  let reviewIdempotencyKey = null;

  const AUDIT_LABELS = {
    input_imported: ['候选人数据进入系统', 'input'],
    ai_analysis_completed: ['AI 建议已生成', 'ai'],
    ai_analysis_failed: ['AI 分析失败', 'warning'],
    hr_review_submitted: ['HR 已完成复核', 'human']
  };

  function showNoData(message) {
    document.getElementById('loadingData').classList.add('hidden');
    document.getElementById('candidateContent').classList.add('hidden');
    document.getElementById('noDataMessage').textContent = message;
    document.getElementById('noData').classList.remove('hidden');
  }

  function renderEvidenceQuotes(item) {
    const sourceQuotes = Array.isArray(item.evidence_quotes)
      ? item.evidence_quotes
      : (typeof item.evidence === 'string' && item.evidence.trim()
        ? [item.evidence]
        : []);
    const quotes = sourceQuotes.map(quote => {
      if (typeof quote === 'string') {
        return { text: quote, valid: item.evidence_valid !== false };
      }
      return {
        text: String((quote && (quote.text || quote.quote)) || '').trim(),
        valid: quote && quote.valid !== false
      };
    }).filter(quote => quote.text);
    if (quotes.length === 0) {
      return '<div class="evidence-block" style="margin-top:6px;color:var(--muted)">未找到可验证的原文证据</div>';
    }
    return quotes.map((quote, index) => `
      <div class="evidence-block" style="margin-top:6px;${quote.valid === false ? 'border-left-color:var(--danger);' : ''}">
        <strong>证据 ${index + 1}</strong>${quote.valid === false ? ' <span style="color:var(--danger)">无法定位</span>' : ''}<br>
        ${escapeHtml(quote.text)}
      </div>
    `).join('');
  }

  function renderCandidate() {
    const candidate = detail.candidate;
    const recommendation = detail.ai_recommendation;
    const review = detail.hr_review;
    document.getElementById('candId').textContent = detail.resume_id;
    document.getElementById('candAiLayer').innerHTML = getLayerTag(recommendation.layer);
    document.getElementById('candLayer').innerHTML = review
      ? getLayerTag(review.final_layer)
      : '<span class="tag tag-unknown">未复核</span>';
    document.getElementById('candMatchScore').innerHTML = getConfidenceBar(recommendation.match_score / 100);
    document.getElementById('candEvidenceConfidence').innerHTML = getConfidenceBar(recommendation.evidence_confidence);

    document.getElementById('analysisBody').innerHTML = recommendation.analysis.map(item => `
      <tr>
        <td><strong>${escapeHtml(item.condition)}</strong></td>
        <td>${getStatusTag(item.status)}</td>
        <td style="font-size:13px;line-height:1.6"><strong>${escapeHtml(item.semantic_relation || '未说明语义关系')}</strong>${renderEvidenceQuotes(item)}</td>
        <td style="font-size:12px;color:var(--muted)">${escapeHtml(item.type)}${item.evidence_valid === false ? '<br><span style="color:var(--danger)">证据无效</span>' : ''}</td>
        <td>${getConfidenceBar(item.match_score / 100)}</td>
        <td>${getConfidenceBar(item.evidence_confidence)}</td>
      </tr>
    `).join('');

    const uncertainties = document.getElementById('uncertainties');
    const risks = document.getElementById('risks');
    uncertainties.innerHTML = recommendation.uncertainties && recommendation.uncertainties.length > 0
      ? '<label>不确定点</label>' + recommendation.uncertainties.map(item =>
        `<div class="evidence-block" style="border-left-color:var(--warning);margin-bottom:8px">${escapeHtml(item)}</div>`
      ).join('')
      : '';
    risks.innerHTML = recommendation.risks && recommendation.risks.length > 0
      ? '<label>风险</label>' + recommendation.risks.map(item =>
        `<div class="evidence-block" style="border-left-color:var(--danger);margin-bottom:8px">${escapeHtml(item)}</div>`
      ).join('')
      : '';
    document.getElementById('uncertaintyCard').classList.toggle(
      'hidden',
      !uncertainties.innerHTML && !risks.innerHTML
    );

    const reviewRecordCard = document.getElementById('reviewRecordCard');
    if (review) {
      reviewRecordCard.classList.remove('hidden');
      document.getElementById('reviewFinalLayer').innerHTML = getLayerTag(review.final_layer);
      document.getElementById('reviewReasonDisplay').textContent = review.reason || '未填写';
      document.getElementById('reviewTimestamp').textContent = new Date(review.reviewed_at).toLocaleString('zh-CN');
    } else {
      reviewRecordCard.classList.add('hidden');
    }

    document.getElementById('resumeText').textContent = candidate.resume_text;
    document.querySelector('input[name="decision"][value="adopt"]').checked = true;
    document.getElementById('newLayer').value = recommendation.layer;
    updateReviewForm('adopt');
    document.getElementById('loadingData').classList.add('hidden');
    document.getElementById('candidateContent').classList.remove('hidden');
  }

  function describeAuditEvent(event) {
    const payload = event && event.payload ? event.payload : {};
    switch (event.event_type) {
      case 'input_imported':
        return `原始简历已导入，来源行为 ${payload.source_row || '未记录'}。`;
      case 'ai_analysis_completed':
        return `系统保存了 AI 建议：${getLayerName(payload.ai_layer)}。模型输出仍需人工复核。`;
      case 'ai_analysis_failed':
        return `该候选人未生成建议，错误代码：${payload.error_code || '未记录'}。`;
      case 'hr_review_submitted':
        return payload.decision === 'override'
          ? `HR 推翻 AI 建议，最终调整为“${getLayerName(payload.final_layer)}”；理由：${payload.reason || '未填写'}。`
          : `HR 采纳 AI 建议，最终结果为“${getLayerName(payload.final_layer)}”。`;
      default:
        return '系统保留了这一过程记录。';
    }
  }

  function renderAuditTrail(events) {
    const status = document.getElementById('auditStatus');
    const timeline = document.getElementById('auditTimeline');
    const visibleEvents = (Array.isArray(events) ? events : [])
      .filter(event => AUDIT_LABELS[event.event_type]);
    if (visibleEvents.length === 0) {
      status.textContent = '当前候选人还没有可展示的审计记录。完成分析或复核后，这里会形成时间线。';
      timeline.classList.add('hidden');
      return;
    }
    timeline.innerHTML = visibleEvents.map(event => {
      const [label, tone] = AUDIT_LABELS[event.event_type];
      const createdAt = event.created_at
        ? new Date(event.created_at).toLocaleString('zh-CN')
        : '时间未记录';
      return `
        <li class="audit-event audit-event-${tone}">
          <div class="audit-event-meta">
            <span class="audit-event-source">${escapeHtml(label)}</span>
            <time>${escapeHtml(createdAt)}</time>
          </div>
          <p>${escapeHtml(describeAuditEvent(event))}</p>
          <code>${escapeHtml(event.event_id || 'event-id unavailable')}</code>
        </li>
      `;
    }).join('');
    status.textContent = `已找到 ${visibleEvents.length} 条与该候选人有关的原始记录。`;
    timeline.classList.remove('hidden');
  }

  async function loadAuditTrail() {
    try {
      const audit = await ScreeningApi.getAuditTrail(jobId, resumeId);
      renderAuditTrail(audit.events);
    } catch (error) {
      document.getElementById('auditStatus').textContent =
        `审计记录暂时无法读取：${error.message || '未知错误'}`;
      document.getElementById('auditTimeline').classList.add('hidden');
    }
  }

  function updateReviewForm(decision) {
    const isOverride = decision === 'override';
    const newLayerGroup = document.getElementById('newLayerGroup');
    const newLayer = document.getElementById('newLayer');
    const reason = document.getElementById('reviewReason');
    const aiLayer = detail && detail.ai_recommendation
      ? detail.ai_recommendation.layer
      : '';

    newLayerGroup.classList.toggle('hidden', !isOverride);
    Array.from(newLayer.options).forEach(option => {
      option.disabled = isOverride && option.value === aiLayer;
    });
    if (isOverride && newLayer.value === aiLayer) {
      const alternative = Array.from(newLayer.options).find(option => !option.disabled);
      if (alternative) newLayer.value = alternative.value;
    }

    document.getElementById('reviewReasonLabel').textContent = isOverride
      ? '复核理由（必填）'
      : '复核理由（选填）';
    reason.placeholder = isOverride
      ? '请填写推翻 AI 建议的原因'
      : '可填写采纳 AI 建议的原因';
    reason.required = isOverride;
  }

  async function submitReview() {
    const decision = document.querySelector('input[name="decision"]:checked').value;
    const reason = document.getElementById('reviewReason').value.trim();
    if (decision === 'override' && !reason) {
      showToast('请填写复核理由');
      return;
    }
    if (!reviewIdempotencyKey) {
      reviewIdempotencyKey = ScreeningState.createIdempotencyKey(
        'review', jobId, resumeId, String(detail.review_version)
      );
    }
    const finalLayer = decision === 'adopt'
      ? detail.ai_recommendation.layer
      : document.getElementById('newLayer').value;
    if (decision === 'override' && finalLayer === detail.ai_recommendation.layer) {
      showToast('推翻 AI 建议时请选择不同的分层');
      return;
    }
    const button = document.getElementById('submitReviewBtn');
    ScreeningFeedback.setButtonLoading(button, true, '正在提交复核...');
    try {
      await ScreeningApi.submitReview(jobId, {
        resume_id: resumeId,
        decision,
        final_layer: finalLayer,
        reason,
        expected_version: detail.review_version,
        idempotency_key: reviewIdempotencyKey
      });
      showToast('复核已保存到后端');
      global.location.href = 'results.html';
    } catch (error) {
      ScreeningFeedback.showApiError(error, '提交复核失败：');
      ScreeningFeedback.setButtonLoading(button, false);
    }
  }

  async function loadCandidate() {
    jobId = ScreeningState.getCurrentJobId();
    const params = new URLSearchParams(global.location.search);
    resumeId = params.get('id') || ScreeningState.getCurrentResumeId();
    if (!jobId || !resumeId) {
      showNoData('请先从筛选结果页选择候选人。');
      return;
    }
    try {
      detail = await ScreeningApi.getCandidate(jobId, resumeId);
      if (detail.analysis_status !== 'completed' || !detail.ai_recommendation) {
        showNoData('该候选人的分析失败，无法生成复核内容。');
        return;
      }
      ScreeningState.saveCurrentResumeId(resumeId);
      renderCandidate();
      await loadAuditTrail();
    } catch (error) {
      ScreeningFeedback.showApiError(error, '加载候选人失败：');
      showNoData(error.message || '未找到候选人数据。');
    }
  }

  document.querySelectorAll('input[name="decision"]').forEach(radio => {
    radio.addEventListener('change', event => {
      updateReviewForm(event.target.value);
    });
  });

  document.getElementById('backToResultsFromEmptyBtn').addEventListener('click', () => {
    global.location.href = 'results.html';
  });
  document.getElementById('backToResultsBtn').addEventListener('click', () => {
    global.location.href = 'results.html';
  });
  document.getElementById('submitReviewBtn').addEventListener('click', submitReview);
  loadCandidate();
})(window);
