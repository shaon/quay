import {Label} from '@patternfly/react-core';

export default function ManifestDigest(props: ManifestDigestProps) {
  const separator = props.digest.indexOf(':');
  const alg = separator >= 0 ? props.digest.slice(0, separator) : 'digest';
  const hash =
    separator >= 0 ? props.digest.slice(separator + 1) : props.digest;
  const condensedHash = hash.slice(0, 14);
  return (
    <>
      <Label color="blue" isCompact>
        {alg}
      </Label>
      {condensedHash}
    </>
  );
}

interface ManifestDigestProps {
  digest: string;
}
