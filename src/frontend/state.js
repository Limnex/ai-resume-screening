(function (global) {
  const KEYS = {
    jobId: 'currentJobId',
    resumeId: 'currentResumeId',
    criteriaVersion: 'currentCriteriaVersion'
  };

  function getCurrentJobId() {
    return localStorage.getItem(KEYS.jobId) || '';
  }

  function saveCurrentJobId(jobId) {
    localStorage.setItem(KEYS.jobId, jobId);
  }

  function getCurrentResumeId() {
    return localStorage.getItem(KEYS.resumeId) || '';
  }

  function saveCurrentResumeId(resumeId) {
    localStorage.setItem(KEYS.resumeId, resumeId);
  }

  function saveCurrentCriteriaVersion(version) {
    localStorage.setItem(KEYS.criteriaVersion, String(version));
  }

  function clearCurrentNavigation() {
    Object.values(KEYS).forEach(key => localStorage.removeItem(key));
  }

  function createIdempotencyKey(prefix, ...parts) {
    const suffix = global.crypto && typeof global.crypto.randomUUID === 'function'
      ? global.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return [prefix, ...parts, suffix].filter(Boolean).join('-');
  }

  global.ScreeningState = Object.freeze({
    getCurrentJobId,
    saveCurrentJobId,
    getCurrentResumeId,
    saveCurrentResumeId,
    saveCurrentCriteriaVersion,
    clearCurrentNavigation,
    createIdempotencyKey
  });
})(window);
