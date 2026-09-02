import {Composition} from 'remotion';
import {CreatorVideo, type CreatorVideoProps} from './video';

const defaultProps: CreatorVideoProps = {
  topic: 'SoloScale Creator Video',
  sourceLabel: 'Operator-supplied claim ledger',
  subtitle: 'Evidence-backed local story',
  width: 1080,
  height: 1920,
  scenes: [
    {
      id: 'SCENE-01',
      start_second: 0,
      end_second: 6,
      purpose: 'Hook',
      voiceover: 'A grounded creator workflow starts from evidence.',
      on_screen_text: 'VERIFIED · CLAIM-01',
      claim_ids: ['CLAIM-01'],
    },
  ],
};

export const RemotionRoot = () => (
  <Composition
    id="CreatorVideo"
    component={CreatorVideo}
    durationInFrames={180}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={defaultProps}
    calculateMetadata={({props}) => ({
      durationInFrames: Math.max(
        30,
        Math.max(...props.scenes.map((scene) => scene.end_second)) * 30,
      ),
      width: props.width ?? 1080,
      height: props.height ?? 1920,
    })}
  />
);
