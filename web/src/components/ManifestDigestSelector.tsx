import {ManifestDigestIdentity} from 'src/resources/TagResource';
import {
  manifestDigestIdentities,
  preferredManifestDigest,
} from 'src/libs/manifestDigests';
import ManifestDigest from './ManifestDigest';

export default function ManifestDigestSelector(
  props: ManifestDigestSelectorProps,
) {
  const identities = manifestDigestIdentities(
    props.identities,
    props.legacyDigest,
  );
  const selected =
    props.selectedDigest ??
    preferredManifestDigest(props.identities, props.legacyDigest);

  if (identities.length === 0) {
    return <span>No registered digest identities</span>;
  }

  if (identities.length === 1) {
    return (
      <span title={identities[0].digest}>
        <ManifestDigest digest={identities[0].digest} />
        {!identities[0].is_enabled && ' (disabled)'}
      </span>
    );
  }

  return (
    <select
      aria-label="Manifest digest identity"
      value={selected ?? ''}
      onChange={(event) => props.onSelect?.(event.target.value)}
    >
      {!selected && <option value="">No enabled digest identity</option>}
      {identities.map((identity) => (
        <option
          key={identity.digest}
          value={identity.digest}
          disabled={!identity.is_enabled && !props.allowDisabled}
        >
          {identity.algorithm}:{identity.digest.split(':').slice(1).join(':')}
          {!identity.is_enabled ? ' (disabled)' : ''}
        </option>
      ))}
    </select>
  );
}

type ManifestDigestSelectorProps = {
  identities?: ManifestDigestIdentity[];
  legacyDigest: string;
  selectedDigest?: string;
  onSelect?: (digest: string) => void;
  allowDisabled?: boolean;
};
