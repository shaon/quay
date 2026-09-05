import {ManifestLinkComponent} from './manifest-link.component';

describe('ManifestLinkComponent digest formatting', () => {
  const component: any = new ManifestLinkComponent(null, null);

  it('renders the registered digest algorithm without assuming SHA-256', () => {
    expect(component.hasDigest('sha512:abcdef')).toBe(true);
    expect(component.getAlgorithm('sha512:abcdef')).toBe('sha512');
  });

  it('shortens only the encoded digest value', () => {
    expect(component.getShortDigest('sha384:1234567890abcdef')).toBe(
      '1234567890ab',
    );
  });
});
