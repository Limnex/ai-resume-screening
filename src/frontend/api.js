(function (global) {
  const configuredBase = global.APP_CONFIG && global.APP_CONFIG.apiBase
    ? global.APP_CONFIG.apiBase
    : document.documentElement.dataset.apiBase || '/api/v1';
  const API_BASE = configuredBase.replace(/\/$/, '');

  class ApiError extends Error {
    constructor(code, message, details = {}, requestId = null, status = 0) {
      super(message);
      this.name = 'ApiError';
      this.code = code;
      this.details = details;
      this.requestId = requestId;
      this.status = status;
    }
  }

  async function request(path, options = {}) {
    const requestOptions = {
      method: options.method || 'GET',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'X-Request-ID': `req-${global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : Date.now()}`,
        ...(options.headers || {})
      }
    };

    if (options.body instanceof FormData) {
      requestOptions.body = options.body;
    } else if (options.body !== undefined) {
      requestOptions.headers['Content-Type'] = 'application/json';
      requestOptions.body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, requestOptions);
    } catch (error) {
      throw new ApiError('NETWORK_ERROR', '无法连接后端服务，请确认服务已启动。');
    }

    const responseText = await response.text();
    let payload = {};
    if (responseText) {
      try {
        payload = JSON.parse(responseText);
      } catch (error) {
        throw new ApiError(
          'INVALID_SERVER_RESPONSE',
          '后端返回了无法识别的内容。',
          {},
          response.headers.get('X-Request-ID'),
          response.status
        );
      }
    }

    if (!response.ok) {
      const serverError = payload.error || {};
      throw new ApiError(
        serverError.code || `HTTP_${response.status}`,
        serverError.message || '后端请求失败。',
        serverError.details || {},
        payload.request_id || response.headers.get('X-Request-ID'),
        response.status
      );
    }
    return payload;
  }

  const ScreeningApi = {
    async previewImport(file) {
      const form = new FormData();
      form.append('resume_file', file);
      return request('/import-previews', { method: 'POST', body: form });
    },

    async createJob(file, selectedRole, candidateLimit = 20) {
      const form = new FormData();
      form.append('resume_file', file);
      form.append('selected_role', selectedRole);
      form.append('candidate_limit', String(candidateLimit));
      return request('/screening-jobs', { method: 'POST', body: form });
    },

    async createJobFromPreview(previewId, selectedRole, candidateLimit = 20) {
      const form = new FormData();
      form.append('preview_id', previewId);
      form.append('selected_role', selectedRole);
      form.append('candidate_limit', String(candidateLimit));
      return request('/screening-jobs', { method: 'POST', body: form });
    },

    getJob(jobId) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}`);
    },

    extractCriteria(jobId) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/criteria/extract`, { method: 'POST' });
    },

    getCriteria(jobId) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/criteria`);
    },

    confirmCriteria(jobId, criteria) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/criteria`, {
        method: 'PUT',
        body: { criteria }
      });
    },

    startAnalysis(jobId, criteriaVersion, idempotencyKey) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/analysis`, {
        method: 'POST',
        body: { criteria_version: criteriaVersion, idempotency_key: idempotencyKey }
      });
    },

    getAnalysisStatus(jobId) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/analysis/status`);
    },

    getResults(jobId, layer, page = 1, pageSize = 100) {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
        sort: 'match_score_desc'
      });
      if (layer) params.set('layer', layer);
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/results?${params}`);
    },

    getCandidate(jobId, resumeId) {
      return request(
        `/screening-jobs/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(resumeId)}`
      );
    },

    getAuditTrail(jobId, resumeId = '') {
      const params = new URLSearchParams();
      if (resumeId) params.set('resume_id', resumeId);
      const suffix = params.toString() ? `?${params}` : '';
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/audit-trail${suffix}`);
    },

    submitReview(jobId, review) {
      return request(`/screening-jobs/${encodeURIComponent(jobId)}/reviews`, {
        method: 'POST',
        body: review
      });
    }
  };

  global.ApiError = ApiError;
  global.ScreeningApi = ScreeningApi;
})(window);
