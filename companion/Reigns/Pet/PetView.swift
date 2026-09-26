import SwiftUI

/// The horse peeks up from the bottom edge (Snapchat-Bitmoji style). The panel's bottom edge is the
/// "floor": the horse is drawn below it and clipped, and rises further with each level (FR-A6).
/// Eyes, eyebrows and mouth are separate views so expressions can swap them independently.
struct PetView: View {
    let model: PetViewModel

    /// The horse is drawn in 64×84 base points, then scaled up.
    static let scale: CGFloat = 2
    static let panelSize = CGSize(width: 96 * scale, height: 104 * scale)
    private static let baseHorseSize = CGSize(width: 64, height: 84)
    private static var horseSize: CGSize {
        CGSize(width: baseHorseSize.width * scale, height: baseHorseSize.height * scale)
    }

    /// Height of the visible part of the horse above the floor, in screen points.
    static func visibleHeight(level: Int) -> CGFloat {
        baseVisibleHeight(level: level) * scale
    }

    /// How much of the horse (from its ear tips down, in base points) shows above the floor.
    private static func baseVisibleHeight(level: Int) -> CGFloat {
        switch level {
        case ..<1: return 44   // Calm: ears + eyes
        case 1: return 54      // Curious
        case 2: return 64      // Concerned: down to the muzzle
        case 3: return 76      // Alarmed
        default: return baseHorseSize.height  // Meltdown: fully out
        }
    }

    var body: some View {
        HorseFace()
            .frame(width: Self.baseHorseSize.width, height: Self.baseHorseSize.height)
            .overlay(alignment: .topTrailing) {
                HeatBadge(heat: model.heat).offset(x: 14, y: -2)
            }
            .scaleEffect(Self.scale)
            .frame(width: Self.horseSize.width, height: Self.horseSize.height)
            .offset(y: (Self.baseHorseSize.height - Self.baseVisibleHeight(level: model.level)) * Self.scale)
            .animation(.spring(response: 0.45, dampingFraction: 0.65), value: model.level)
            .frame(width: Self.panelSize.width, height: Self.panelSize.height, alignment: .bottom)
            .clipped()
    }
}

private enum HorsePalette {
    static let coat = Color(red: 0.55, green: 0.34, blue: 0.20)
    static let mane = Color(red: 0.22, green: 0.13, blue: 0.08)
    static let muzzle = Color(red: 0.80, green: 0.64, blue: 0.50)
    static let innerEar = Color(red: 0.85, green: 0.62, blue: 0.55)
}

/// Drawn in a 64×84 frame with the ear tips at the top edge.
private struct HorseFace: View {
    var body: some View {
        ZStack {
            // Ears sit behind the head.
            HStack(spacing: 18) {
                Ear()
                Ear()
            }
            .offset(y: -33)

            // Head: tall rounded shape.
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .fill(HorsePalette.coat)
                .frame(width: 46, height: 70)
                .offset(y: 6)

            // Forelock tuft between the ears.
            Ellipse()
                .fill(HorsePalette.mane)
                .frame(width: 20, height: 14)
                .offset(y: -26)

            // Muzzle with nostrils.
            Ellipse()
                .fill(HorsePalette.muzzle)
                .frame(width: 42, height: 28)
                .offset(y: 26)
            HStack(spacing: 14) {
                Nostril()
                Nostril()
            }
            .offset(y: 24)

            HorseMouth().offset(y: 34)

            HStack(spacing: 18) {
                Eye()
                Eye()
            }
            .offset(y: -6)

            HStack(spacing: 18) {
                Eyebrow()
                Eyebrow()
            }
            .offset(y: -16)
        }
    }
}

private struct Ear: View {
    var body: some View {
        ZStack {
            Triangle().fill(HorsePalette.coat).frame(width: 14, height: 18)
            Triangle().fill(HorsePalette.innerEar).frame(width: 7, height: 10).offset(y: 3)
        }
    }
}

private struct Eye: View {
    var body: some View {
        ZStack {
            Circle().fill(Color.white).frame(width: 13, height: 13)
            Circle().fill(Color.black).frame(width: 7, height: 7)
            Circle().fill(Color.white).frame(width: 2.5, height: 2.5).offset(x: 1.5, y: -1.5)
        }
    }
}

private struct Eyebrow: View {
    var body: some View {
        Capsule().fill(HorsePalette.mane).frame(width: 10, height: 2.5)
    }
}

private struct Nostril: View {
    var body: some View {
        Ellipse().fill(HorsePalette.mane.opacity(0.8)).frame(width: 5, height: 7)
    }
}

/// Calm: small smile.
private struct HorseMouth: View {
    var body: some View {
        Path { p in
            p.move(to: CGPoint(x: 0, y: 0))
            p.addQuadCurve(to: CGPoint(x: 14, y: 0), control: CGPoint(x: 7, y: 5))
        }
        .stroke(HorsePalette.mane, style: StrokeStyle(lineWidth: 1.8, lineCap: .round))
        .frame(width: 14, height: 5)
    }
}

private struct Triangle: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.midX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
            p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
            p.closeSubpath()
        }
    }
}

/// FR-A7: heat number always visible so color is never the only signal.
private struct HeatBadge: View {
    let heat: Int

    var body: some View {
        Text("\(heat)")
            .font(.system(size: 11, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.black.opacity(0.7)))
    }
}
