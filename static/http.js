/* window.pdcFetch — the one request helper of the /lab and dashboard pages.
 *
 * A TRANSPARENT pass-through to fetch: every argument is forwarded exactly as
 * given (streamed bodies, blobs, headers, credentials, abort signals) and the
 * original Response comes back untouched — never read, cloned or rebuilt —
 * for every status but one.
 *
 * On 401 the session is gone (signed out elsewhere, ended by an
 * administrator, expired): the browser goes to the sign-in page with the
 * current path as ?next= (the sign-in page ignores it) and the returned
 * promise never settles, so no caller shows an error for it. The one
 * exception is /auth/password, whose 401 means "wrong current password":
 * that answer goes back to the caller like any other.
 *
 * Loaded before every other local script of the two pages.
 */
(function () {
  'use strict';

  var PASSWORD_PATH = '/auth/password';

  function requestPath(input) {
    try {
      var raw = (typeof input === 'string') ? input
        : (input && input.url) ? input.url : String(input);
      return new URL(raw, window.location.href).pathname;
    } catch (e) {
      return '';
    }
  }

  function isPasswordChange(input) {
    var path = requestPath(input);
    return path === PASSWORD_PATH || path.indexOf(PASSWORD_PATH + '/') === 0;
  }

  window.pdcFetch = function (...args) {
    return fetch(...args).then(function (res) {
      if (res.status === 401 && !isPasswordChange(args[0])) {
        window.location.assign('/?next=' + encodeURIComponent(
          window.location.pathname + window.location.search));
        return new Promise(function () {});
      }
      return res;
    });
  };
})();
