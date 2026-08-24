// Wird von vitest vor jeder Testdatei geladen (siehe `test.setupFiles` in
// vite.config.ts).
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// jsdom haelt das document ueber Testgrenzen hinweg. Ohne dieses Aufraeumen
// faende `getByRole` Elemente aus dem vorigen Test und die Fehlermeldung
// lautete "found multiple elements" statt der eigentlichen Ursache.
afterEach(cleanup);

// Blob-URLs gibt es in jsdom nicht. Die Panels rufen sie beim Verwerfen eines
// Dateiergebnisses auf (`gibErgebnisFrei`), sonst schluege jeder Test darueber
// fehl, ohne dass es mit dem Prueflung zu tun haette.
if (!URL.createObjectURL) {
  URL.createObjectURL = () => 'blob:test';
}
if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = () => undefined;
}
