import React from 'react';
import ReactDOM from 'react-dom/client';
import { createHashRouter, RouterProvider } from 'react-router';
import ReaderApp from './App';
import { ReaderProvider } from './reader-state';
import './index.css';

const router = createHashRouter([
  { path: '*', element: <ReaderProvider><ReaderApp /></ReaderProvider> },
]);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><RouterProvider router={router} /></React.StrictMode>,
);
