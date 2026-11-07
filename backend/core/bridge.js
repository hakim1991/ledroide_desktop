(function () {
  var pending   = new Map();
  var listeners = new Map();

  function makeError(payload) {
    var err = new Error(payload.error);
    err.code = payload.code;
    return err;
  }

  window.addEventListener('message', function (e) {
    if (e.source !== window.parent) return;
    var d = e.data || {};
    if (d.type === 'ledroide:response' && pending.has(d.requestId)) {
      var p = pending.get(d.requestId);
      clearTimeout(p.timer);
      pending.delete(d.requestId);
      if (d.payload && d.payload.error) p.reject(makeError(d.payload));
      else p.resolve(d.payload);
    }
    if (d.type === 'ledroide:event') {
      var cbs = listeners.get(d.moduleId + ':' + d.action);
      if (cbs) cbs.forEach(function (cb) { cb(d.payload); });
    }
  });

  var ledroide = {
    backendPort: parseInt(window.location.port) || 8765,

    // options.timeout in ms (default 10000, 0 = none)
    call: function (moduleId, action, payload, options) {
      payload = payload || {};
      var timeout = options && options.timeout !== undefined ? options.timeout : 10000;
      return new Promise(function (resolve, reject) {
        var rid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
          : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
        var timer = timeout ? setTimeout(function () {
          pending.delete(rid);
          var err = new Error('RPC timeout');
          err.code = 'RPC_TIMEOUT';
          reject(err);
        }, timeout) : null;
        pending.set(rid, { resolve: resolve, reject: reject, timer: timer });
        window.parent.postMessage(
          { type: 'ledroide:call', moduleId: moduleId, action: action, payload: payload, requestId: rid, timeout: timeout },
          '*'
        );
      });
    },

    // Long-running action registered with @module.job: resolves with the job result.
    // options.onProgress({percent, message, ...}), options.onStart(jobId)
    run: function (moduleId, action, payload, options) {
      options = options || {};
      return new Promise(function (resolve, reject) {
        var jobId = null;
        var buffered = [];
        function handle(kind, d) {
          if (jobId === null) { buffered.push([kind, d]); return; }
          if (!d || d.job_id !== jobId) return;
          if (kind === 'progress') { if (options.onProgress) options.onProgress(d); return; }
          cleanup();
          if (kind === 'done') resolve(d.result);
          else reject(makeError(d));
        }
        var onProgress = function (d) { handle('progress', d); };
        var onDone     = function (d) { handle('done', d); };
        var onError    = function (d) { handle('error', d); };
        function cleanup() {
          ledroide.off(moduleId, 'job:progress', onProgress);
          ledroide.off(moduleId, 'job:done', onDone);
          ledroide.off(moduleId, 'job:error', onError);
        }
        ledroide.on(moduleId, 'job:progress', onProgress);
        ledroide.on(moduleId, 'job:done', onDone);
        ledroide.on(moduleId, 'job:error', onError);
        ledroide.call(moduleId, action, payload).then(function (r) {
          jobId = r && r.job_id;
          if (!jobId) { cleanup(); resolve(r); return; }
          if (options.onStart) options.onStart(jobId);
          var queue = buffered; buffered = [];
          queue.forEach(function (b) { handle(b[0], b[1]); });
        }, function (err) { cleanup(); reject(err); });
      });
    },

    cancel: function (moduleId, jobId) {
      return ledroide.call(moduleId, 'job:cancel', { job_id: jobId });
    },

    on: function (moduleId, event, callback) {
      var key = moduleId + ':' + event;
      if (!listeners.has(key)) {
        listeners.set(key, new Set());
        window.parent.postMessage({ type: 'ledroide:subscribe', moduleId: moduleId, event: event }, '*');
      }
      listeners.get(key).add(callback);
    },

    off: function (moduleId, event, callback) {
      var cbs = listeners.get(moduleId + ':' + event);
      if (cbs) cbs.delete(callback);
    },
  };

  window.ledroide = ledroide;
})();
