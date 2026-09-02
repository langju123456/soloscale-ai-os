import React from 'react';
import {
  AbsoluteFill,
  Audio,
  interpolate,
  OffthreadVideo,
  Sequence,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

export type CreatorScene = {
  id: string;
  start_second: number;
  end_second: number;
  purpose: string;
  voiceover: string;
  on_screen_text: string;
  claim_ids: string[];
  visual_kind?:
    | 'hook'
    | 'pipeline'
    | 'separation'
    | 'implementation'
    | 'metrics'
    | 'bottleneck'
    | 'evolution';
  detail_lines?: string[];
  audio_data_url?: string | null;
  audio_asset?: string | null;
  avatar_clip_asset?: string | null;
  presenter_layout?: 'FULL_FRAME' | 'PICTURE_IN_PICTURE' | 'SIDE_PANEL' | null;
};

export type CreatorVideoProps = {
  topic: string;
  sourceLabel: string;
  subtitle?: string;
  scenes: CreatorScene[];
  width?: number;
  height?: number;
};

const palette = ['#7c83fd', '#61a5fa', '#38bdf8', '#a78bfa', '#fb7185'];

const Diagram: React.FC<{scene: CreatorScene; accent: string; progress: number}> = ({
  scene,
  accent,
  progress,
}) => {
  const lines = scene.detail_lines ?? [];
  if (scene.visual_kind === 'hook') {
    return (
      <div style={{display: 'grid', placeItems: 'center', height: 510}}>
        <div
          style={{
            background: 'rgba(8,17,36,.86)',
            border: '2px solid rgba(255,255,255,.18)',
            borderRadius: 42,
            boxShadow: `0 30px 90px ${accent}44`,
            height: 380,
            overflow: 'hidden',
            transform: `scale(${0.92 + progress * 0.08})`,
            width: 760,
          }}
        >
          <div style={{background: 'rgba(255,255,255,.08)', display: 'flex', gap: 14, padding: 24}}>
            {['#fb7185', '#fbbf24', '#34d399'].map((color) => (
              <span key={color} style={{background: color, borderRadius: 99, height: 18, width: 18}} />
            ))}
          </div>
          <div style={{display: 'grid', gap: 30, padding: '65px 48px', textAlign: 'center'}}>
            <div style={{fontSize: 98, fontWeight: 900, letterSpacing: -4}}>{lines[0] ?? '~2 MIN'}</div>
            <div style={{color: '#cbd5e1', fontSize: 34, fontWeight: 700}}>{lines[1]}</div>
            <div style={{height: 14, background: 'rgba(255,255,255,.1)', borderRadius: 99}}>
              <div style={{height: '100%', width: `${20 + progress * 55}%`, borderRadius: 99, background: accent}} />
            </div>
          </div>
        </div>
      </div>
    );
  }
  if (scene.visual_kind === 'pipeline' || scene.visual_kind === 'implementation') {
    return (
      <div style={{display: 'grid', gap: 22, marginTop: 64}}>
        {lines.map((line, index) => (
          <div key={line} style={{display: 'flex', alignItems: 'center', gap: 24, opacity: index <= Math.floor(progress * lines.length) ? 1 : 0.35}}>
            <div style={{background: accent, borderRadius: 99, color: '#081124', display: 'grid', fontSize: 24, fontWeight: 900, height: 48, placeItems: 'center', width: 48}}>{index + 1}</div>
            <div style={{background: 'rgba(255,255,255,.09)', border: '1px solid rgba(255,255,255,.16)', borderRadius: 22, flex: 1, fontSize: 34, fontWeight: 800, padding: '22px 30px'}}>{line}</div>
          </div>
        ))}
      </div>
    );
  }
  if (scene.visual_kind === 'separation') {
    return (
      <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, marginTop: 82}}>
        {lines.map((line, index) => (
          <div key={line} style={{background: index % 2 ? `${accent}28` : 'rgba(255,255,255,.08)', border: `1px solid ${accent}66`, borderRadius: 28, fontSize: 29, fontWeight: 850, minHeight: 160, padding: 30, display: 'grid', placeItems: 'center', textAlign: 'center', transform: `translateY(${(1 - progress) * (index % 2 ? 35 : -35)}px)`}}>{line}</div>
        ))}
      </div>
    );
  }
  if (scene.visual_kind === 'metrics') {
    return (
      <div style={{display: 'grid', gap: 30, marginTop: 92}}>
        {lines.map((line, index) => {
          const [label, post] = line.split(' · ');
          return (
            <div key={line} style={{background: 'rgba(255,255,255,.09)', border: `1px solid ${accent}66`, borderRadius: 34, padding: '34px 38px'}}>
              <div style={{color: accent, fontSize: 28, fontWeight: 900}}>{index === 0 ? 'COLD RUN' : 'WARM RUN'}</div>
              <div style={{fontSize: 48, fontWeight: 900, marginTop: 16}}>{label.replace(index === 0 ? 'COLD ' : 'WARM ', '')}</div>
              <div style={{color: '#cbd5e1', fontSize: 29, fontWeight: 700, marginTop: 14}}>{post}</div>
            </div>
          );
        })}
      </div>
    );
  }
  if (scene.visual_kind === 'bottleneck') {
    return (
      <div style={{display: 'grid', placeItems: 'center', marginTop: 85}}>
        <div style={{border: `22px solid ${accent}`, borderRadius: 999, boxShadow: `0 0 90px ${accent}55`, display: 'grid', height: 390, placeItems: 'center', width: 390}}>
          <div style={{fontSize: 94, fontWeight: 950}}>&gt;99%</div>
        </div>
        <div style={{display: 'grid', gap: 14, marginTop: 45, width: '100%'}}>
          {lines.map((line) => <div key={line} style={{color: '#cbd5e1', fontSize: 29, fontWeight: 750, textAlign: 'center'}}>{line}</div>)}
        </div>
      </div>
    );
  }
  return (
    <div style={{display: 'grid', gap: 24, marginTop: 110}}>
      {lines.map((line, index) => (
        <div key={line} style={{background: index === lines.length - 1 ? accent : 'rgba(255,255,255,.09)', color: index === lines.length - 1 ? '#081124' : '#f8fafc', borderRadius: 30, fontSize: 32, fontWeight: 900, padding: 32, textAlign: 'center'}}>{line}</div>
      ))}
    </div>
  );
};

export const CreatorVideo: React.FC<CreatorVideoProps> = ({topic, sourceLabel, subtitle, scenes}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const landscape = width > height;
  const second = frame / fps;
  const sceneIndex = Math.min(
    scenes.length - 1,
    Math.max(0, scenes.findIndex((scene) => second >= scene.start_second && second < scene.end_second)),
  );
  const scene = scenes[sceneIndex] ?? scenes[0];
  const localFrame = frame - scene.start_second * fps;
  const sceneFrames = Math.max(1, (scene.end_second - scene.start_second) * fps);
  const opacity = interpolate(localFrame, [0, 12, sceneFrames - 15, sceneFrames], [0, 1, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const translateY = interpolate(localFrame, [0, 20], [40, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const accent = palette[sceneIndex % palette.length];
  const entrance = spring({fps, frame: Math.max(0, localFrame), config: {damping: 18, stiffness: 105}});
  const progress = Math.min(1, Math.max(0, localFrame / Math.max(1, sceneFrames - 1)));
  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at 84% 10%, ${accent}aa, #10182f 38%, #07101f 75%)`,
        color: '#f8fafc',
        fontFamily: 'Arial, sans-serif',
        padding: landscape ? '50px 72px 46px' : '74px 70px 68px',
      }}
    >
      {scenes.map((item) => item.audio_data_url || item.audio_asset ? (
        <Sequence key={item.id} from={item.start_second * fps} durationInFrames={(item.end_second - item.start_second) * fps}>
          <Audio src={item.audio_data_url ?? staticFile(item.audio_asset as string)} volume={0.94} />
        </Sequence>
      ) : null)}
      {scenes.map((item) => item.avatar_clip_asset ? (
        <Sequence
          key={`avatar-${item.id}`}
          from={item.start_second * fps}
          durationInFrames={(item.end_second - item.start_second) * fps}
        >
          <div
            style={{
              position: 'absolute',
              right: item.presenter_layout === 'FULL_FRAME' ? 0 : item.presenter_layout === 'SIDE_PANEL' ? 0 : landscape ? 72 : 70,
              top: item.presenter_layout === 'FULL_FRAME' || item.presenter_layout === 'SIDE_PANEL' ? 0 : landscape ? 170 : 510,
              width: item.presenter_layout === 'FULL_FRAME' ? '100%' : item.presenter_layout === 'SIDE_PANEL' ? (landscape ? 620 : 540) : landscape ? 520 : 760,
              height: item.presenter_layout === 'FULL_FRAME' || item.presenter_layout === 'SIDE_PANEL' ? '100%' : landscape ? 600 : 760,
              overflow: 'hidden',
              borderRadius: item.presenter_layout === 'FULL_FRAME' || item.presenter_layout === 'SIDE_PANEL' ? 0 : 36,
              border: item.presenter_layout === 'FULL_FRAME' ? 'none' : `2px solid ${accent}99`,
              boxShadow: `0 30px 90px ${accent}44`,
              zIndex: item.presenter_layout === 'FULL_FRAME' ? 0 : 2,
            }}
          >
            <OffthreadVideo
              src={staticFile(item.avatar_clip_asset)}
              muted={Boolean(item.audio_data_url || item.audio_asset)}
              style={{width: '100%', height: '100%', objectFit: 'cover'}}
            />
          </div>
        </Sequence>
      ) : null)}
      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 24, fontWeight: 850, letterSpacing: 2.4, opacity: 0.82}}>
        <span>SOLOSCALE · ENGINEERING STORY</span>
        <span>{String(sceneIndex + 1).padStart(2, '0')} / {String(scenes.length).padStart(2, '0')}</span>
      </div>
      <div style={{marginTop: landscape ? 34 : 70, fontSize: landscape ? 38 : 48, lineHeight: 1.13, fontWeight: 900, letterSpacing: -1.5}}>{topic}</div>
      {subtitle ? <div style={{color: '#cbd5e1', fontSize: 26, lineHeight: 1.35, marginTop: 18}}>{subtitle}</div> : null}
      <div
        style={{
          marginTop: landscape ? 34 : 58,
          opacity,
          transform: `translateY(${translateY}px) scale(${0.96 + entrance * 0.04})`,
          maxWidth: landscape && scene.avatar_clip_asset ? 1080 : undefined,
        }}
      >
        <div style={{color: accent, fontSize: 25, fontWeight: 900, letterSpacing: 2.2, textTransform: 'uppercase'}}>{scene.purpose}</div>
        <div style={{fontSize: landscape ? 46 : 58, fontWeight: 900, lineHeight: 1.16, marginTop: 22}}>{scene.on_screen_text}</div>
        {!scene.avatar_clip_asset ? <Diagram scene={scene} accent={accent} progress={progress} /> : null}
      </div>
      <div style={{marginTop: 'auto', background: 'rgba(2,8,23,.82)', border: '1px solid rgba(255,255,255,.15)', borderRadius: 26, fontSize: landscape ? 26 : 31, fontWeight: 760, lineHeight: 1.52, padding: landscape ? '18px 28px' : '24px 30px', textAlign: 'center', zIndex: 3}}>
        {scene.voiceover}
      </div>
      <div style={{display: 'flex', justifyContent: 'space-between', marginTop: 22, fontSize: 20, opacity: 0.63}}>
        <span>{scene.claim_ids.join(' · ') || 'CTA'}</span><span>{sourceLabel}</span>
      </div>
    </AbsoluteFill>
  );
};
