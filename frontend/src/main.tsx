import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import ErrorBoundary, { installGlobalErrorHandlers } from './ErrorBoundary';
import './styles.css';

// Show exact errors instead of a silent blackout on any crash.
installGlobalErrorHandlers();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
