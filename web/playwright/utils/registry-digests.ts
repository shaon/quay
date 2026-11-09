/**
 * Raw registry helpers for configurable digest algorithms (PROJQUAY-10117).
 *
 * Pushes and pulls use raw HTTP requests, not container tooling, so the exact bytes and
 * digests are under the test's control.
 */

import {createHash, randomBytes} from 'crypto';
import {APIRequestContext, APIResponse} from '@playwright/test';
import type {QuayConfig} from '../fixtures';
import {API_URL} from './config';

export type DigestAlgorithm = 'sha256' | 'sha384' | 'sha512';
export const DIGEST_ALGORITHMS: DigestAlgorithm[] = [
  'sha256',
  'sha384',
  'sha512',
];

export const OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json';
const OCI_CONFIG = 'application/vnd.oci.image.config.v1+json';
const OCI_LAYER = 'application/vnd.oci.image.layer.v1.tar+gzip';

export function digestOf(
  algorithm: DigestAlgorithm,
  data: Buffer | string,
): string {
  return `${algorithm}:${createHash(algorithm).update(data).digest('hex')}`;
}

/** sha256 is always allowed; others only when listed in ALLOWED_HASH_ALGORITHMS. */
export function isAlgorithmAllowed(
  quayConfig: QuayConfig | undefined,
  algorithm: DigestAlgorithm,
): boolean {
  if (algorithm === 'sha256') return true;
  const allowed = quayConfig?.config?.ALLOWED_HASH_ALGORITHMS;
  return Array.isArray(allowed) && allowed.includes(algorithm);
}

export interface Descriptor {
  digest: string;
  size: number;
}

export interface TestImage {
  config: Buffer;
  layer: Buffer;
}

export function newTestImage(label: string): TestImage {
  return {
    config: Buffer.from(
      JSON.stringify({
        architecture: 'amd64',
        os: 'linux',
        config: {Labels: {test: label}},
        rootfs: {type: 'layers', diff_ids: []},
      }),
    ),
    layer: randomBytes(512),
  };
}

export function ociManifest(config: Descriptor, layers: Descriptor[]): Buffer {
  return Buffer.from(
    JSON.stringify({
      schemaVersion: 2,
      mediaType: OCI_MANIFEST,
      config: {mediaType: OCI_CONFIG, digest: config.digest, size: config.size},
      layers: layers.map((l) => ({
        mediaType: OCI_LAYER,
        digest: l.digest,
        size: l.size,
      })),
    }),
  );
}

function auth(token: string): Record<string, string> {
  return {authorization: `Bearer ${token}`};
}

/** Monolithic upload: POST /blobs/uploads/?digest= with the whole body (blob upload path A). */
export async function uploadBlobMonolithic(
  request: APIRequestContext,
  repo: string,
  token: string,
  data: Buffer,
  digest: string,
): Promise<APIResponse> {
  return request.post(
    `${API_URL}/v2/${repo}/blobs/uploads/?digest=${encodeURIComponent(digest)}`,
    {
      headers: {
        ...auth(token),
        'Content-Type': 'application/octet-stream',
      },
      data,
    },
  );
}

/**
 * Chunked upload: POST (optionally with a digest-algorithm hint), two PATCH chunks, then a
 * final PUT with the digest (blob upload paths B and C). Returns the final response.
 */
export async function uploadBlobChunked(
  request: APIRequestContext,
  repo: string,
  token: string,
  data: Buffer,
  digest: string,
  hint?: DigestAlgorithm,
): Promise<APIResponse> {
  const query = hint ? `?digest-algorithm=${hint}` : '';
  const start = await request.post(
    `${API_URL}/v2/${repo}/blobs/uploads/${query}`,
    {headers: auth(token)},
  );
  if (start.status() !== 202) return start;

  let location = start.headers()['location'];
  const half = Math.floor(data.length / 2);
  const chunks: Array<[number, Buffer]> = [
    [0, data.subarray(0, half)],
    [half, data.subarray(half)],
  ];
  for (const [offset, chunk] of chunks) {
    const patch = await request.patch(location, {
      headers: {
        ...auth(token),
        'Content-Type': 'application/octet-stream',
        'Content-Range': `${offset}-${offset + chunk.length - 1}`,
      },
      data: chunk,
    });
    if (patch.status() !== 202) return patch;
    location = patch.headers()['location'];
  }

  const separator = location.includes('?') ? '&' : '?';
  return request.put(
    `${location}${separator}digest=${encodeURIComponent(digest)}`,
    {headers: auth(token)},
  );
}

export async function putManifest(
  request: APIRequestContext,
  repo: string,
  token: string,
  reference: string,
  body: Buffer,
  tags: string[] = [],
): Promise<APIResponse> {
  const query = tags.map((t) => `tag=${encodeURIComponent(t)}`).join('&');
  return request.put(
    `${API_URL}/v2/${repo}/manifests/${reference}${query ? `?${query}` : ''}`,
    {
      headers: {...auth(token), 'Content-Type': OCI_MANIFEST},
      data: body,
    },
  );
}

export async function getManifest(
  request: APIRequestContext,
  repo: string,
  token: string,
  reference: string,
  method: 'GET' | 'HEAD' = 'GET',
): Promise<APIResponse> {
  return request.fetch(`${API_URL}/v2/${repo}/manifests/${reference}`, {
    method,
    headers: {...auth(token), Accept: OCI_MANIFEST},
  });
}

export async function getBlob(
  request: APIRequestContext,
  repo: string,
  token: string,
  digest: string,
  method: 'GET' | 'HEAD' = 'GET',
): Promise<APIResponse> {
  return request.fetch(`${API_URL}/v2/${repo}/blobs/${digest}`, {
    method,
    headers: auth(token),
  });
}

export function headerValues(response: APIResponse, name: string): string[] {
  return response
    .headersArray()
    .filter((h) => h.name.toLowerCase() === name.toLowerCase())
    .flatMap((h) => h.value.split(',').map((v) => v.trim()));
}

export async function errorCode(response: APIResponse): Promise<string> {
  const body = await response.json();
  return body.errors?.[0]?.code;
}

/**
 * Pushes an image whose blobs and manifest use `algorithm` and tags it. Returns the digests.
 */
export async function pushImageByDigest(
  request: APIRequestContext,
  repo: string,
  token: string,
  algorithm: DigestAlgorithm,
  image: TestImage,
  tags: string[],
): Promise<{
  manifest: Buffer;
  manifestDigest: string;
  layerDigest: string;
  configDigest: string;
}> {
  const configDigest = digestOf(algorithm, image.config);
  const layerDigest = digestOf(algorithm, image.layer);
  const configUpload = await uploadBlobMonolithic(
    request,
    repo,
    token,
    image.config,
    configDigest,
  );
  if (configUpload.status() !== 201) {
    throw new Error(`config upload failed: ${configUpload.status()}`);
  }
  const layerUpload = await uploadBlobChunked(
    request,
    repo,
    token,
    image.layer,
    layerDigest,
    algorithm,
  );
  if (layerUpload.status() !== 201) {
    throw new Error(`layer upload failed: ${layerUpload.status()}`);
  }
  const manifest = ociManifest(
    {digest: configDigest, size: image.config.length},
    [{digest: layerDigest, size: image.layer.length}],
  );
  const manifestDigest = digestOf(algorithm, manifest);
  const put = await putManifest(
    request,
    repo,
    token,
    manifestDigest,
    manifest,
    tags,
  );
  if (put.status() !== 201) {
    throw new Error(
      `manifest push failed: ${put.status()} ${await put.text()}`,
    );
  }
  return {manifest, manifestDigest, layerDigest, configDigest};
}
