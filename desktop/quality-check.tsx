import React from 'react';
import { createRoot } from 'react-dom/client';
import { Editor } from './src/pages/Editor';
import { getBackendUrl } from './src/lib/backend';
import './src/globals.css';
window.electron = { getBackendUrl: async () => 'http://127.0.0.1:8081' } as any;
await getBackendUrl();
delete (window as any).electron;
createRoot(document.getElementById('root')!).render(<Editor />);
