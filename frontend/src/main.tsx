import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// Dark-Mode-Praeferenz VOR dem ersten Render anwenden — sonst blitzt die helle
// Oberflaeche kurz auf, bevor React die Klasse setzt.
const gespeichertesThema = localStorage.getItem('pti-theme');
if (
  gespeichertesThema === 'dark' ||
  (!gespeichertesThema && window.matchMedia('(prefers-color-scheme: dark)').matches)
) {
  document.documentElement.classList.add('dark');
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
