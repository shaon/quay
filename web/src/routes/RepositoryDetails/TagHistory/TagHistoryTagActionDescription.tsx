import {Label} from '@patternfly/react-core';
import {TagAction, TagEntry} from './types';
import ManifestDigestSelector from 'src/components/ManifestDigestSelector';
import {useState} from 'react';

export default function TagActionDescription({tagEntry}: {tagEntry: TagEntry}) {
  const [digest, setDigest] = useState(tagEntry.digest);
  const [oldDigest, setOldDigest] = useState(tagEntry.oldDigest);
  const selectDigest = (selected: string) => {
    tagEntry.digest = selected;
    setDigest(selected);
  };
  const selectOldDigest = (selected: string) => {
    tagEntry.oldDigest = selected;
    setOldDigest(selected);
  };
  const currentIdentity = (
    <ManifestDigestSelector
      identities={tagEntry.digestIdentities}
      legacyDigest={digest}
      selectedDigest={digest}
      onSelect={selectDigest}
    />
  );
  const oldIdentity = oldDigest ? (
    <ManifestDigestSelector
      identities={tagEntry.oldDigestIdentities}
      legacyDigest={oldDigest}
      selectedDigest={oldDigest}
      onSelect={selectOldDigest}
    />
  ) : null;

  switch (tagEntry.action) {
    case TagAction.Create:
      return (
        <>
          <Label isCompact>{tagEntry.tag.name}</Label> was created pointing to{' '}
          {currentIdentity}
        </>
      );
    case TagAction.Recreate:
      return (
        <>
          <Label isCompact>{tagEntry.tag.name}</Label> was recreated pointing to{' '}
          {currentIdentity}
        </>
      );
    case TagAction.Delete:
      return (
        <>
          <Label isCompact>{tagEntry.tag.name}</Label>{' '}
          {tagEntry.time >= new Date().getTime()
            ? `will expire`
            : `was deleted`}
        </>
      );
    case TagAction.Revert:
      return (
        <>
          <Label isCompact>{tagEntry.tag.name}</Label> was reverted to{' '}
          {currentIdentity} from {oldIdentity}
        </>
      );
    case TagAction.Move:
      return (
        <>
          <Label isCompact>{tagEntry.tag.name}</Label> was moved to{' '}
          {currentIdentity} from {oldIdentity}
        </>
      );
  }
}
