import {describe, expect, it} from 'vitest';
import {
  manifestDigestIdentities,
  preferredManifestDigest,
  retainedManifestDigest,
  tagNavigationDigest,
} from './manifestDigests';

const identities = [
  {
    digest: 'sha512:disabled',
    algorithm: 'sha512',
    is_enabled: false,
    is_preferred: false,
  },
  {
    digest: 'sha384:preferred',
    algorithm: 'sha384',
    is_enabled: true,
    is_preferred: true,
  },
];

describe('manifest digest identities', () => {
  it('uses the explicit preferred enabled registration', () => {
    expect(preferredManifestDigest(identities, 'sha256:legacy')).toBe(
      'sha384:preferred',
    );
  });

  it('does not fall back when the new API returns an empty list', () => {
    expect(preferredManifestDigest([], 'sha256:legacy')).toBeUndefined();
    expect(manifestDigestIdentities([], 'sha256:legacy')).toEqual([]);
  });

  it('falls back only when talking to an old API', () => {
    expect(preferredManifestDigest(undefined, 'sha256:legacy')).toBe(
      'sha256:legacy',
    );
  });

  it('keeps manifest-list architecture navigation separate from root identities', () => {
    expect(tagNavigationDigest(true, 'sha512:root')).toBeUndefined();
    expect(tagNavigationDigest(false, 'sha512:manifest')).toBe(
      'sha512:manifest',
    );
  });

  it('selects an explicit disabled registration only for retained cleanup', () => {
    expect(
      retainedManifestDigest(
        [
          {
            digest: 'sha512:retained',
            algorithm: 'sha512',
            is_enabled: false,
            is_preferred: false,
          },
        ],
        'sha256:unregistered',
      ),
    ).toBe('sha512:retained');
    expect(retainedManifestDigest([], 'sha256:unregistered')).toBeUndefined();
  });
});
