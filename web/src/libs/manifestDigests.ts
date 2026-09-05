import {ManifestDigestIdentity} from 'src/resources/TagResource';

export function legacyManifestDigestIdentity(
  digest: string,
): ManifestDigestIdentity[] {
  if (!digest) {
    return [];
  }
  return [
    {
      digest,
      algorithm: digest.split(':', 1)[0],
      is_enabled: true,
      is_preferred: true,
    },
  ];
}

export function manifestDigestIdentities(
  identities: ManifestDigestIdentity[] | undefined,
  legacyDigest: string,
): ManifestDigestIdentity[] {
  return identities === undefined
    ? legacyManifestDigestIdentity(legacyDigest)
    : identities;
}

export function preferredManifestDigest(
  identities: ManifestDigestIdentity[] | undefined,
  legacyDigest: string,
): string | undefined {
  const available = manifestDigestIdentities(identities, legacyDigest).filter(
    (identity) => identity.is_enabled,
  );
  return (
    available.find((identity) => identity.is_preferred)?.digest ??
    available[0]?.digest
  );
}

export function tagNavigationDigest(
  isManifestList: boolean,
  selectedDigest: string | undefined,
): string | undefined {
  return isManifestList ? undefined : selectedDigest;
}

export function retainedManifestDigest(
  identities: ManifestDigestIdentity[] | undefined,
  legacyDigest: string,
): string | undefined {
  const retained = manifestDigestIdentities(identities, legacyDigest);
  return (
    retained.find((identity) => identity.is_enabled && identity.is_preferred)
      ?.digest ??
    retained.find((identity) => identity.is_enabled)?.digest ??
    retained[0]?.digest
  );
}
