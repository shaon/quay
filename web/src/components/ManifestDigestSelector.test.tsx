import {fireEvent, render, screen} from '@testing-library/react';
import {describe, expect, it, vi} from 'vitest';
import ManifestDigestSelector from './ManifestDigestSelector';

const identities = [
  {
    digest: 'sha256:disabled',
    algorithm: 'sha256',
    is_enabled: false,
    is_preferred: false,
  },
  {
    digest: 'sha384:preferred',
    algorithm: 'sha384',
    is_enabled: true,
    is_preferred: true,
  },
  {
    digest: 'sha512:enabled',
    algorithm: 'sha512',
    is_enabled: true,
    is_preferred: false,
  },
];

describe('ManifestDigestSelector', () => {
  it('selects the preferred enabled identity and disables unavailable identities', () => {
    const onSelect = vi.fn();
    render(
      <ManifestDigestSelector
        identities={identities}
        legacyDigest="sha256:legacy"
        onSelect={onSelect}
      />,
    );

    const selector = screen.getByRole('combobox');
    expect(selector).toHaveValue('sha384:preferred');
    expect(
      screen.getByRole('option', {name: /sha256:disabled/}),
    ).toBeDisabled();
    fireEvent.change(selector, {target: {value: 'sha512:enabled'}});
    expect(onSelect).toHaveBeenCalledWith('sha512:enabled');
  });

  it('distinguishes an explicit empty inventory from an old API', () => {
    const {rerender} = render(
      <ManifestDigestSelector identities={[]} legacyDigest="sha256:legacy" />,
    );
    expect(screen.getByText('No registered digest identities')).toBeVisible();

    rerender(
      <ManifestDigestSelector
        identities={undefined}
        legacyDigest="sha256:legacy"
      />,
    );
    expect(screen.getByTitle('sha256:legacy')).toBeVisible();
  });
});
