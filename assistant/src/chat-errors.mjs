export const failureStatus = Object.freeze({
  round_limit:422, read_limit:422, deadline:504, provider_timeout:504,
  provider_network:502, provider_http:502, billing_stopped:503,
  billing_unverified:503, auth_expired:401, tool_failed:502, invalid_reply:502,
});
export class ChatFailure extends Error {
  constructor(reason) { super(reason);this.reason=Object.hasOwn(failureStatus,reason)?reason:"tool_failed"; }
}
export function safeFailure(error,aborted=false) {
  const reason=aborted?"deadline":error instanceof ChatFailure?error.reason:"tool_failed";
  return {status:failureStatus[reason],body:{error:reason}};
}
