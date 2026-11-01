import { cp, lstat } from 'node:fs/promises';
import path from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

function publishReaderData(): Plugin {
  let source = '';
  let output = '';
  return {
    name: 'publish-reader-data',
    apply: 'build',
    configResolved(config) {
      source = path.join(config.publicDir, 'data');
      output = path.resolve(config.root, config.build.outDir, 'data');
    },
    async writeBundle() {
      // Docker builds the frontend before public data is staged at runtime.
      let info;
      try { info = await lstat(source); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
        throw error;
      }
      if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Reader public data must be a real directory.');
      await cp(source, output, { recursive: true });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), publishReaderData()],
  publicDir: '.reader-site',
  base: process.env.READER_BASE_PATH || './',
  build: { target: 'es2022', sourcemap: false, copyPublicDir: false },
  server: { host: '127.0.0.1', strictPort: true },
  preview: { host: '127.0.0.1', strictPort: true },
});
