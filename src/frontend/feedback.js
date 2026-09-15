(function (global) {
  function showApiError(error, prefix = '') {
    const requestInfo = error.requestId ? `（请求编号：${error.requestId}）` : '';
    const detailInfo = typeof global.formatErrorDetails === 'function'
      ? global.formatErrorDetails(error.details)
      : '';
    const message = `${prefix}${error.message || '请求失败'}${detailInfo ? `；${detailInfo}` : ''}${requestInfo}`;
    console.error(error);
    if (typeof global.showToast === 'function') global.showToast(message, 6000);
    else global.alert(message);
  }

  function setButtonLoading(button, loading, loadingText = '处理中...') {
    if (!button) return;
    if (loading) {
      if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
      button.disabled = true;
      button.textContent = loadingText;
    } else {
      button.disabled = false;
      button.textContent = button.dataset.originalText || button.textContent;
    }
  }

  global.ScreeningFeedback = Object.freeze({ showApiError, setButtonLoading });
})(window);
