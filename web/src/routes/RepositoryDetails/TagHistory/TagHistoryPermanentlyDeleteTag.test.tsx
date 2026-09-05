import {render, screen, userEvent} from 'src/test-utils';
import {TagAction, TagEntry} from './types';
import PermanentlyDeleteTag from './TagHistoryPermanentlyDeleteTag';

const permanentlyDeleteTag = vi.fn();
vi.mock('src/hooks/UseTags', () => ({
  usePermanentlyDeleteTag: () => ({
    permanentlyDeleteTag,
    success: false,
    error: null,
  }),
}));

const entry: TagEntry = {
  action: TagAction.Delete,
  time: Date.now() - 1000,
  tag: {name: 'latest', manifest_digest: 'sha256:unregistered'} as any,
  digest: 'sha256:unregistered',
  digestIdentities: [
    {
      digest: 'sha512:retained',
      algorithm: 'sha512',
      is_enabled: false,
      is_preferred: false,
    },
  ],
  canRestoreDigest: false,
  cleanupDigest: 'sha512:retained',
  oldDigest: null,
};

describe('TagHistoryPermanentlyDeleteTag', () => {
  it('uses an explicit disabled registration for destructive cleanup', async () => {
    render(<PermanentlyDeleteTag org="acme" repo="widget" tagEntry={entry} />);

    await userEvent.click(screen.getByText(/Delete/, {selector: 'a'}));
    await userEvent.click(
      screen.getByRole('button', {name: 'Permanently delete tag'}),
    );

    expect(permanentlyDeleteTag).toHaveBeenCalledWith({
      tag: 'latest',
      digest: 'sha512:retained',
    });
  });
});
