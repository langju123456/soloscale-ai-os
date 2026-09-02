import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';
import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import process from 'node:process';

const args = process.argv.slice(2);
const valueFor = (flag) => {
  const index = args.indexOf(flag);
  if (index === -1 || !args[index + 1]) {
    throw new Error(`Missing ${flag}`);
  }
  return args[index + 1];
};

try {
  const input = resolve(valueFor('--input'));
  const output = resolve(valueFor('--output'));
  const thumbnailIndex = args.indexOf('--thumbnail');
  const thumbnail = thumbnailIndex === -1 ? null : resolve(valueFor('--thumbnail'));
  const publicIndex = args.indexOf('--public-dir');
  const publicDir = publicIndex === -1 ? null : resolve(valueFor('--public-dir'));
  const inputProps = JSON.parse(await readFile(input, 'utf8'));
  const serveUrl = await bundle({
    entryPoint: resolve('src/entry.ts'),
    // A packaged macOS app must remain immutable after signing. Remotion's
    // default Webpack cache lives under node_modules/.cache, which would write
    // into the signed app bundle at runtime and invalidate its signature.
    enableCaching: false,
    ...(publicDir ? {publicDir} : {}),
  });
  const browserExecutable = process.env.REMOTION_BROWSER_EXECUTABLE;
  const composition = await selectComposition({
    serveUrl,
    id: 'CreatorVideo',
    inputProps,
    browserExecutable,
  });
  await renderMedia({
    composition,
    serveUrl,
    codec: 'h264',
    outputLocation: output,
    inputProps,
    overwrite: false,
    concurrency: 1,
    logLevel: 'error',
    browserExecutable,
  });
  if (thumbnail) {
    await renderStill({
      composition,
      serveUrl,
      output: thumbnail,
      inputProps,
      frame: 18,
      imageFormat: 'png',
      overwrite: false,
      browserExecutable,
    });
  }
  process.stdout.write(`${output}\n`);
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
