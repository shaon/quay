import {render, screen} from 'src/test-utils';
import CopyTags from './DetailsCopyTags';

vi.mock('src/hooks/UseQuayConfig', () => ({
  useQuayConfig: () => ({config: {SERVER_HOSTNAME: 'quay.example'}}),
}));

describe('DetailsCopyTags manifest identity handling', () => {
  it('uses the selected enabled identity in digest pull commands', () => {
    render(
      <CopyTags
        org="acme"
        repo="widget"
        tag="latest"
        digest="sha512:registered"
      />,
    );

    expect(
      screen.getByTestId('podman-digest-clipboardcopy').querySelector('input'),
    ).toHaveValue('podman pull quay.example/acme/widget@sha512:registered');
    expect(
      screen.getByTestId('docker-digest-clipboardcopy').querySelector('input'),
    ).toHaveValue('docker pull quay.example/acme/widget@sha512:registered');
  });

  it('omits digest pull commands when no enabled registration exists', () => {
    render(<CopyTags org="acme" repo="widget" tag="latest" digest="" />);

    expect(
      screen.queryByTestId('podman-digest-clipboardcopy'),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('docker-digest-clipboardcopy'),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('podman-tag-clipboardcopy')).toBeVisible();
  });
});
