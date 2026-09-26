import AVFoundation
import Foundation

/// FR-V1/FR-V2 (Role B's engine voice): plays voice.play audio. Silent while muted or paused.
@MainActor
final class VoicePlayer {
    private var player: AVAudioPlayer?

    func play(_ voice: VoicePlay) {
        guard let data = Data(base64Encoded: voice.audioB64) else {
            Log.net.error("voice.play: audio isn't valid base64")
            return
        }
        do {
            let player = try AVAudioPlayer(data: data)
            player.play()
            self.player = player  // keep it alive until it finishes
            Log.net.info("Speaking: \(voice.text, privacy: .public)")
        } catch {
            Log.net.error("voice.play: can't play audio (\(error.localizedDescription, privacy: .public))")
        }
    }

    func stop() {
        player?.stop()
        player = nil
    }
}
