import '@testing-library/jest-dom/vitest';

process.env.FRONTEND_SESSION_SECRET = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';

HTMLDialogElement.prototype.showModal = function showModal(): void {
  this.setAttribute('open', '');
};

HTMLDialogElement.prototype.close = function close(): void {
  this.removeAttribute('open');
  this.dispatchEvent(new Event('close'));
};
