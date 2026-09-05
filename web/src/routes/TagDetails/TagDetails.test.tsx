import {MemoryRouter} from 'react-router-dom';
import {render, screen, waitFor} from 'src/test-utils';
import TagDetails from './TagDetails';
import {getManifestByDigest, getTags} from 'src/resources/TagResource';

vi.mock('src/hooks/UseQuayConfig', () => ({
  useQuayConfig: () => ({features: {UI_MODELCARD: false}}),
}));
vi.mock('src/components/breadcrumb/Breadcrumb', () => ({
  QuayBreadcrumb: () => null,
}));
vi.mock('./TagDetailsArchSelect', () => ({
  default: ({digest}: {digest: string}) => (
    <div data-testid="selected-architecture">{digest}</div>
  ),
}));
vi.mock('./TagDetailsTabs', () => ({
  default: ({
    digest,
    manifestReference,
    manifestData,
  }: {
    digest: string;
    manifestReference: string;
    manifestData: {digest: string};
  }) => (
    <div data-testid="selected-manifest">
      {manifestReference}|{digest}|{manifestData?.digest}
    </div>
  ),
}));
vi.mock('src/resources/TagResource', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('src/resources/TagResource')>();
  return {
    ...actual,
    getTags: vi.fn(),
    getManifestByDigest: vi.fn(),
  };
});

const mockGetTags = getTags as ReturnType<typeof vi.fn>;
const mockGetManifest = getManifestByDigest as ReturnType<typeof vi.fn>;

describe('TagDetails manifest identity selection', () => {
  it('loads the selected child manifest inventory instead of treating its descriptor as a legacy identity', async () => {
    mockGetTags.mockResolvedValue({
      tags: [
        {
          name: 'latest',
          is_manifest_list: true,
          manifest_digest: 'sha256:canonical-root',
          manifest_digests: [
            {
              digest: 'sha384:root',
              algorithm: 'sha384',
              is_enabled: true,
              is_preferred: true,
            },
          ],
          child_manifests_presence: {'sha256:child-descriptor': true},
        },
      ],
    });
    mockGetManifest
      .mockResolvedValueOnce({
        digest: 'sha384:root',
        manifest_digests: [
          {
            digest: 'sha384:root',
            algorithm: 'sha384',
            is_enabled: true,
            is_preferred: true,
          },
        ],
        manifest_data: JSON.stringify({
          manifests: [
            {
              digest: 'sha256:child-descriptor',
              platform: {os: 'linux', architecture: 'amd64'},
            },
          ],
        }),
      })
      .mockResolvedValueOnce({
        digest: 'sha256:child-descriptor',
        manifest_digests: [
          {
            digest: 'sha512:child-registration',
            algorithm: 'sha512',
            is_enabled: true,
            is_preferred: true,
          },
        ],
        manifest_data: '{}',
        layers: [],
      });

    render(
      <MemoryRouter initialEntries={['/repository/acme/widget/tag/latest']}>
        <TagDetails />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByTestId('selected-manifest')).toHaveTextContent(
        'sha256:child-descriptor|sha512:child-registration|sha256:child-descriptor',
      ),
    );
    expect(screen.getByTestId('selected-architecture')).toHaveTextContent(
      'sha256:child-descriptor',
    );
    expect(mockGetManifest).toHaveBeenNthCalledWith(
      2,
      'acme',
      'widget',
      'sha256:child-descriptor',
    );
  });
});
